# Copyright 2018 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Asynchronous data producer for the NCF pipeline."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import atexit
import collections
import functools
import os
import pickle
import struct
import sys
import tempfile
import threading
import time
import timeit
import traceback

import numpy as np
import six
from six.moves import queue
import tensorflow as tf
from tensorflow.contrib.tpu.python.tpu.datasets import StreamingFilesDataset

from official.datasets import movielens
from official.recommendation import constants as rconst
from official.recommendation import popen_helper
from official.recommendation import stat_utils


SUMMARY_TEMPLATE = """General:
{spacer}Num users: {num_users}
{spacer}Num items: {num_items}

Training:
{spacer}Positive count:          {train_pos_ct}
{spacer}Batch size:              {train_batch_size} {multiplier}
{spacer}Batch count per epoch:   {train_batch_ct}

Eval:
{spacer}Positive count:          {eval_pos_ct}
{spacer}Batch size:              {eval_batch_size} {multiplier}
{spacer}Batch count per epoch:   {eval_batch_ct}"""


_TRAIN_FEATURE_MAP = {
    movielens.USER_COLUMN: tf.FixedLenFeature([], dtype=tf.string),
    movielens.ITEM_COLUMN: tf.FixedLenFeature([], dtype=tf.string),
    rconst.MASK_START_INDEX: tf.FixedLenFeature([1], dtype=tf.string),
    "labels": tf.FixedLenFeature([], dtype=tf.string),
}


_EVAL_FEATURE_MAP = {
    movielens.USER_COLUMN: tf.FixedLenFeature([], dtype=tf.string),
    movielens.ITEM_COLUMN: tf.FixedLenFeature([], dtype=tf.string),
    rconst.DUPLICATE_MASK: tf.FixedLenFeature([], dtype=tf.string)
}


class DatasetManager(object):
  """Helper class for handling TensorFlow specific data tasks.

  This class takes the (relatively) framework agnostic work done by the data
  constructor classes and handles the TensorFlow specific portions (TFRecord
  management, tf.Dataset creation, etc.).
  """
  def __init__(self, is_training, stream_files, batches_per_epoch,
               shard_root=None):
    self._is_training = is_training
    self._stream_files = stream_files
    self._writers = []
    self._write_locks = [threading.RLock() for _ in
                         range(rconst.NUM_FILE_SHARDS)] if stream_files else []
    self._batches_per_epoch = batches_per_epoch
    self._epochs_completed = 0
    self._epochs_requested = 0
    self._shard_root = shard_root

    self._result_queue = queue.Queue()
    self._result_reuse = []

  @property
  def current_data_root(self):
    subdir = (rconst.TRAIN_FOLDER_TEMPLATE.format(self._epochs_completed)
              if self._is_training else rconst.EVAL_FOLDER)
    return os.path.join(self._shard_root, subdir)

  def buffer_reached(self):
    # Only applicable for training.
    return (self._epochs_completed - self._epochs_requested >=
            rconst.CYCLES_TO_BUFFER and self._is_training)

  @staticmethod
  def _serialize(data):
    """Convert NumPy arrays into a TFRecords entry."""

    feature_dict = {
        k: tf.train.Feature(bytes_list=tf.train.BytesList(
            value=[memoryview(v).tobytes()])) for k, v in data.items()}

    return tf.train.Example(
        features=tf.train.Features(feature=feature_dict)).SerializeToString()

  def _deserialize(self, serialized_data, batch_size):
    """Convert serialized TFRecords into tensors.

    Args:
      serialized_data: A tensor containing serialized records.
      batch_size: The data arrives pre-batched, so batch size is needed to
        deserialize the data.
    """
    feature_map = _TRAIN_FEATURE_MAP if self._is_training else _EVAL_FEATURE_MAP
    features = tf.parse_single_example(serialized_data, feature_map)

    users = tf.reshape(tf.decode_raw(
        features[movielens.USER_COLUMN], rconst.USER_DTYPE), (batch_size,))
    items = tf.reshape(tf.decode_raw(
        features[movielens.ITEM_COLUMN], rconst.ITEM_DTYPE), (batch_size,))

    def decode_binary(data_bytes):
      # tf.decode_raw does not support bool as a decode type. As a result it is
      # necessary to decode to int8 (7 of the bits will be ignored) and then
      # cast to bool.
      return tf.reshape(tf.cast(tf.decode_raw(data_bytes, tf.int8), tf.bool),
                        (batch_size,))

    if self._is_training:
      mask_start_index = tf.decode_raw(
          features[rconst.MASK_START_INDEX], tf.int32)[0]
      valid_point_mask = tf.less(tf.range(batch_size), mask_start_index)

      return {
          movielens.USER_COLUMN: users,
          movielens.ITEM_COLUMN: items,
          rconst.VALID_POINT_MASK: valid_point_mask,
      }, decode_binary(features["labels"])

    return {
        movielens.USER_COLUMN: users,
        movielens.ITEM_COLUMN: items,
        rconst.DUPLICATE_MASK: decode_binary(features[rconst.DUPLICATE_MASK]),
    }

  def put(self, index, data):
    # type: (int, dict) -> None
    """Store data for later consumption.

    Because there are several paths for storing and yielding data (queues,
    lists, files) the data producer simply provides the data in a standard
    format at which point the dataset manager handles storing it in the correct
    form.

    Args:
      index: Used to select shards when writing to files.
      data: A dict of the data to be stored. This method mutates data, and
        therefore expects to be the only consumer.
    """

    if self._stream_files:
      example_bytes = self._serialize(data)
      with self._write_locks[index % rconst.NUM_FILE_SHARDS]:
        self._writers[index % rconst.NUM_FILE_SHARDS].write(example_bytes)

    else:
      if self._is_training:
        mask_start_index = data.pop(rconst.MASK_START_INDEX)
        batch_size = data[movielens.ITEM_COLUMN].shape[0]
        data[rconst.VALID_POINT_MASK] = np.less(np.arange(batch_size),
                                                mask_start_index)
        self._result_queue.put((data, data.pop("labels")))
      else:
        self._result_reuse.append(data)

  def start_construction(self):
    if self._stream_files:
      tf.gfile.MakeDirs(self.current_data_root)
      template = os.path.join(self.current_data_root, rconst.SHARD_TEMPLATE)
      self._writers = [tf.io.TFRecordWriter(template.format(i))
                       for i in range(rconst.NUM_FILE_SHARDS)]

  def end_construction(self):
    if self._stream_files:
      [writer.close() for writer in self._writers]
      self._writers = []
      self._result_queue.put(self.current_data_root)
    elif not self._is_training:
      self._result_queue.put(True)  # data is ready.

    self._epochs_completed += 1

  def data_generator(self, epochs_between_evals):
    """Yields examples during local training."""
    assert not self._stream_files

    if self._is_training:
      for _ in range(self._batches_per_epoch * epochs_between_evals):
        yield self._result_queue.get(timeout=300)

    else:
      # Evaluation waits for all data to be ready.
      self._result_queue.put(self._result_queue.get(timeout=300))
      assert len(self._result_reuse) == self._batches_per_epoch
      assert epochs_between_evals == 1
      for i in self._result_reuse:
        yield i

  def get_dataset(self, batch_size, epochs_between_evals):
    """Construct the dataset to be used for training and eval.

    For local training, data is provided through Dataset.from_generator. For
    remote training (TPUs) the data is first serialized to files and then sent
    to the TPU through a StreamingFilesDataset.

    Args:
      batch_size: The per-device batch size of the dataset.
      epochs_between_evals: How many epochs worth of data to yield.
        (Generator mode only.)
    """
    self._epochs_requested += 1
    if self._stream_files:
      if epochs_between_evals > 1:
        raise ValueError("epochs_between_evals > 1 not supported for file "
                         "based dataset.")
      epoch_data_dir = self._result_queue.get(timeout=300)
      if not self._is_training:
        self._result_queue.put(epoch_data_dir)  # Eval data is reused.

      file_pattern = os.path.join(
          epoch_data_dir, rconst.SHARD_TEMPLATE.format("*"))
      dataset = StreamingFilesDataset(
          files=file_pattern, worker_job="worker",
          num_parallel_reads=rconst.NUM_FILE_SHARDS, num_epochs=1)
      map_fn = functools.partial(self._deserialize, batch_size=batch_size)
      dataset = dataset.map(map_fn, num_parallel_calls=16)

    else:
      types = {movielens.USER_COLUMN: rconst.USER_DTYPE,
               movielens.ITEM_COLUMN: rconst.ITEM_DTYPE}
      shapes = {movielens.USER_COLUMN: tf.TensorShape([batch_size]),
                movielens.ITEM_COLUMN: tf.TensorShape([batch_size])}

      if self._is_training:
        types[rconst.VALID_POINT_MASK] = np.bool
        shapes[rconst.VALID_POINT_MASK] = tf.TensorShape([batch_size])

        types = (types, np.bool)
        shapes = (shapes, tf.TensorShape([batch_size]))

      else:
        types[rconst.DUPLICATE_MASK] = np.bool
        shapes[rconst.DUPLICATE_MASK] = tf.TensorShape([batch_size])

      data_generator = functools.partial(
          self.data_generator, epochs_between_evals=epochs_between_evals)
      dataset = tf.data.Dataset.from_generator(
          generator=data_generator, output_types=types,
          output_shapes=shapes)

    return dataset.prefetch(16)

  def make_input_fn(self, batch_size):
    """Create an input_fn which checks for batch size consistency."""

    def input_fn(params):
      param_batch_size = (params["batch_size"] if self._is_training else
                          params["eval_batch_size"])
      if batch_size != param_batch_size:
        raise ValueError("producer batch size ({}) differs from params batch "
                         "size ({})".format(batch_size, param_batch_size))

      epochs_between_evals = (params.get("epochs_between_evals", 1)
                              if self._is_training else 1)
      return self.get_dataset(batch_size=batch_size,
                              epochs_between_evals=epochs_between_evals)

    return input_fn


class BaseDataConstructor(threading.Thread):
  """Data constructor base class.

  This class manages the control flow for constructing data. It is not meant
  to be used directly, but instead subclasses should implement the following
  two methods:

    self.construct_lookup_variables
    self.lookup_negative_items

  """
  def __init__(self,
               maximum_number_epochs,   # type: int
               num_users,               # type: int
               num_items,               # type: int
               user_map,                # type: dict
               item_map,                # type: dict
               train_pos_users,         # type: np.ndarray
               train_pos_items,         # type: np.ndarray
               train_batch_size,        # type: int
               batches_per_train_step,  # type: int
               num_train_negatives,     # type: int
               eval_pos_users,          # type: np.ndarray
               eval_pos_items,          # type: np.ndarray
               eval_batch_size,         # type: int
               batches_per_eval_step,   # type: int
               stream_files             # type: bool
              ):
    # General constants
    self._maximum_number_epochs = maximum_number_epochs
    self._num_users = num_users
    self._num_items = num_items
    self.user_map = user_map
    self.item_map = item_map
    self._train_pos_users = train_pos_users
    self._train_pos_items = train_pos_items
    self.train_batch_size = train_batch_size
    self._num_train_negatives = num_train_negatives
    self._batches_per_train_step = batches_per_train_step
    self._eval_pos_users = eval_pos_users
    self._eval_pos_items = eval_pos_items
    self.eval_batch_size = eval_batch_size

    # Training
    if self._train_pos_users.shape != self._train_pos_items.shape:
      raise ValueError(
          "User positives ({}) is different from item positives ({})".format(
              self._train_pos_users.shape, self._train_pos_items.shape))

    self._train_pos_count = self._train_pos_users.shape[0]
    self._elements_in_epoch = (1 + num_train_negatives) * self._train_pos_count
    self.train_batches_per_epoch = self._count_batches(
        self._elements_in_epoch, train_batch_size, batches_per_train_step)

    # Evaluation
    if eval_batch_size % (1 + rconst.NUM_EVAL_NEGATIVES):
      raise ValueError("Eval batch size {} is not divisible by {}".format(
          eval_batch_size, 1 + rconst.NUM_EVAL_NEGATIVES))
    self._eval_users_per_batch = int(
        eval_batch_size // (1 + rconst.NUM_EVAL_NEGATIVES))
    self._eval_elements_in_epoch = num_users * (1 + rconst.NUM_EVAL_NEGATIVES)
    self.eval_batches_per_epoch = self._count_batches(
        self._eval_elements_in_epoch, eval_batch_size, batches_per_eval_step)

    # Intermediate artifacts
    self._current_epoch_order = np.empty(shape=(0,))
    self._shuffle_iterator = None

    if stream_files:
      self._shard_root = tempfile.mkdtemp(prefix="ncf_")
      atexit.register(tf.gfile.DeleteRecursively, dirname=self._shard_root)
    else:
      self._shard_root = None

    self._train_dataset = DatasetManager(
        True, stream_files, self.train_batches_per_epoch, self._shard_root)
    self._eval_dataset = DatasetManager(
        False, stream_files, self.eval_batches_per_epoch, self._shard_root)

    # Threading details
    self._current_epoch_order_lock = threading.RLock()
    super(BaseDataConstructor, self).__init__()
    self.daemon = True
    self._stop_loop = False
    self._fatal_exception = None

  def __repr__(self):
    multiplier = ("(x{} devices)".format(self._batches_per_train_step)
                  if self._batches_per_train_step > 1 else "")
    summary = SUMMARY_TEMPLATE.format(
        spacer="  ", num_users=self._num_users, num_items=self._num_items,
        train_pos_ct=self._train_pos_count,
        train_batch_size=self.train_batch_size,
        train_batch_ct=self.train_batches_per_epoch,
        eval_pos_ct=self._num_users, eval_batch_size=self.eval_batch_size,
        eval_batch_ct=self.eval_batches_per_epoch, multiplier=multiplier)
    return super(BaseDataConstructor, self).__repr__() + "\n" + summary

  @staticmethod
  def _count_batches(example_count, batch_size, batches_per_step):
    x = (example_count + batch_size - 1) // batch_size
    return (x + batches_per_step - 1) // batches_per_step * batches_per_step

  def stop_loop(self):
    self._stop_loop = True

  def _get_order_chunk(self):
    with self._current_epoch_order_lock:
      batch_indices, self._current_epoch_order = (
          self._current_epoch_order[:self.train_batch_size],
          self._current_epoch_order[self.train_batch_size:])

      return batch_indices

  def construct_lookup_variables(self):
    """Perform any one time pre-compute work."""
    raise NotImplementedError

  def lookup_negative_items(self, **kwargs):
    """Randomly sample negative items for given users."""
    raise NotImplementedError

  def _run(self):
    atexit.register(self.stop_loop)
    self._start_shuffle_iterator()
    self.construct_lookup_variables()
    self._construct_training_epoch()
    self._construct_eval_epoch()
    for _ in range(self._maximum_number_epochs - 1):
      self._construct_training_epoch()

  def run(self):
    try:
      self._run()
    except Exception as e:
      # The Thread base class swallows stack traces, so unfortunately it is
      # necessary to catch and re-raise to get debug output
      print(traceback.format_exc(), file=sys.stderr)
      self._fatal_exception = e
      sys.stderr.flush()
      raise

  def _start_shuffle_iterator(self):
    pool = popen_helper.get_forkpool(3, closing=False)
    atexit.register(pool.close)
    args = [(self._elements_in_epoch, stat_utils.random_int32())
            for _ in range(self._maximum_number_epochs)]
    self._shuffle_iterator = pool.imap_unordered(stat_utils.permutation, args)

  def _get_training_batch(self, i):
    """Construct a single batch of training data.

    Args:
      i: The index of the batch. This is used when stream_files=True to assign
        data to file shards.
    """
    batch_indices = self._get_order_chunk()
    mask_start_index = batch_indices.shape[0]

    batch_ind_mod = np.mod(batch_indices, self._train_pos_count)
    users = self._train_pos_users[batch_ind_mod]

    negative_indices = np.greater_equal(batch_indices, self._train_pos_count)
    negative_users = users[negative_indices]

    negative_items = self.lookup_negative_items(negative_users=negative_users)

    items = self._train_pos_items[batch_ind_mod]
    items[negative_indices] = negative_items

    labels = np.logical_not(negative_indices).astype(np.bool)

    # Pad last partial batch
    pad_length = self.train_batch_size - mask_start_index
    if pad_length:
      # We pad with arange rather than zeros because the network will still
      # compute logits for padded examples, and padding with zeros would create
      # a very "hot" embedding key which can have performance implications.
      user_pad = np.arange(pad_length, dtype=users.dtype) % self._num_users
      item_pad = np.arange(pad_length, dtype=items.dtype) % self._num_items
      label_pad = np.zeros(shape=(pad_length,), dtype=labels.dtype)
      users = np.concatenate([users, user_pad])
      items = np.concatenate([items, item_pad])
      labels = np.concatenate([labels, label_pad])

    self._train_dataset.put(i, {
        movielens.USER_COLUMN: users,
        movielens.ITEM_COLUMN: items,
        rconst.MASK_START_INDEX: np.array(mask_start_index, dtype=np.int32),
        "labels": labels,
    })

  def _wait_to_construct_train_epoch(self):
    count = 0
    while self._train_dataset.buffer_reached() and not self._stop_loop:
      time.sleep(0.01)
      count += 1
      if count >= 100 and np.log10(count) == np.round(np.log10(count)):
        tf.logging.info(
            "Waited {} times for training data to be consumed".format(count))

  def _construct_training_epoch(self):
    """Loop to construct a batch of training data."""
    self._wait_to_construct_train_epoch()
    start_time = timeit.default_timer()
    if self._stop_loop:
      return

    self._train_dataset.start_construction()
    map_args = list(range(self.train_batches_per_epoch))
    assert not self._current_epoch_order.shape[0]
    self._current_epoch_order = six.next(self._shuffle_iterator)

    with popen_helper.get_threadpool(6) as pool:
      pool.map(self._get_training_batch, map_args)
    self._train_dataset.end_construction()

    tf.logging.info("Epoch construction complete. Time: {:.1f} seconds".format(
        timeit.default_timer() - start_time))

  @staticmethod
  def _assemble_eval_batch(users, positive_items, negative_items,
                           users_per_batch):
    """Construct duplicate_mask and structure data accordingly.

    The positive items should be last so that they lose ties. However, they
    should not be masked out if the true eval positive happens to be
    selected as a negative. So instead, the positive is placed in the first
    position, and then switched with the last element after the duplicate
    mask has been computed.

    Args:
      users: An array of users in a batch. (should be identical along axis 1)
      positive_items: An array (batch_size x 1) of positive item indices.
      negative_items: An array of negative item indices.
      users_per_batch: How many users should be in the batch. This is passed
        as an argument so that ncf_test.py can use this method.

    Returns:
      User, item, and duplicate_mask arrays.
    """
    items = np.concatenate([positive_items, negative_items], axis=1)

    # We pad the users and items here so that the duplicate mask calculation
    # will include the padding. The metric function relies on every element
    # except the positive being marked as duplicate to mask out padded points.
    if users.shape[0] < users_per_batch:
      pad_rows = users_per_batch - users.shape[0]
      padding = np.zeros(shape=(pad_rows, users.shape[1]), dtype=np.int32)
      users = np.concatenate([users, padding.astype(users.dtype)], axis=0)
      items = np.concatenate([items, padding.astype(items.dtype)], axis=0)

    duplicate_mask = stat_utils.mask_duplicates(items, axis=1).astype(np.bool)

    items[:, (0, -1)] = items[:, (-1, 0)]
    duplicate_mask[:, (0, -1)] = duplicate_mask[:, (-1, 0)]

    assert users.shape == items.shape == duplicate_mask.shape
    return users, items, duplicate_mask

  def _get_eval_batch(self, i):
    """Construct a single batch of evaluation data.

    Args:
      i: The index of the batch.
    """
    low_index = i * self._eval_users_per_batch
    high_index = (i + 1) * self._eval_users_per_batch
    users = np.repeat(self._eval_pos_users[low_index:high_index, np.newaxis],
                      1 + rconst.NUM_EVAL_NEGATIVES, axis=1)
    positive_items = self._eval_pos_items[low_index:high_index, np.newaxis]
    negative_items = (self.lookup_negative_items(negative_users=users[:, :-1])
                      .reshape(-1, rconst.NUM_EVAL_NEGATIVES))

    users, items, duplicate_mask = self._assemble_eval_batch(
        users, positive_items, negative_items, self._eval_users_per_batch)

    self._eval_dataset.put(i, {
        movielens.USER_COLUMN: users.flatten(),
        movielens.ITEM_COLUMN: items.flatten(),
        rconst.DUPLICATE_MASK: duplicate_mask.flatten(),
    })

  def _construct_eval_epoch(self):
    """Loop to construct data for evaluation."""
    if self._stop_loop:
      return

    start_time = timeit.default_timer()

    self._eval_dataset.start_construction()
    map_args = [i for i in range(self.eval_batches_per_epoch)]
    with popen_helper.get_threadpool(6) as pool:
      pool.map(self._get_eval_batch, map_args)
    self._eval_dataset.end_construction()

    tf.logging.info("Eval construction complete. Time: {:.1f} seconds".format(
        timeit.default_timer() - start_time))

  def make_input_fn(self, is_training):
    if self._fatal_exception is not None:
      raise ValueError("Fatal exception in the data production loop: {}"
                       .format(self._fatal_exception))

    return (
        self._train_dataset.make_input_fn(self.train_batch_size) if is_training
        else self._eval_dataset.make_input_fn(self.eval_batch_size))


class DummyConstructor(threading.Thread):
  """Class for running with synthetic data."""
  def run(self):
    pass

  def stop_loop(self):
    pass

  @staticmethod
  def make_input_fn(is_training):
    """Construct training input_fn that uses synthetic data."""

    def input_fn(params):
      """Generated input_fn for the given epoch."""
      batch_size = (params["batch_size"] if is_training else
                    params["eval_batch_size"] or params["batch_size"])
      num_users = params["num_users"]
      num_items = params["num_items"]

      users = tf.random_uniform([batch_size], dtype=tf.int32, minval=0,
                                maxval=num_users)
      items = tf.random_uniform([batch_size], dtype=tf.int32, minval=0,
                                maxval=num_items)

      if is_training:
        valid_point_mask = tf.cast(tf.random_uniform(
            [batch_size], dtype=tf.int32, minval=0, maxval=2), tf.bool)
        labels = tf.cast(tf.random_uniform(
            [batch_size], dtype=tf.int32, minval=0, maxval=2), tf.bool)
        data = {
            movielens.USER_COLUMN: users,
            movielens.ITEM_COLUMN: items,
            rconst.VALID_POINT_MASK: valid_point_mask,
        }, labels
      else:
        dupe_mask = tf.cast(tf.random_uniform([batch_size], dtype=tf.int32,
                                              minval=0, maxval=2), tf.bool)
        data = {
            movielens.USER_COLUMN: users,
            movielens.ITEM_COLUMN: items,
            rconst.DUPLICATE_MASK: dupe_mask,
        }

      dataset = tf.data.Dataset.from_tensors(data).repeat(
          rconst.SYNTHETIC_BATCHES_PER_EPOCH * params["batches_per_step"])
      dataset = dataset.prefetch(32)
      return dataset

    return input_fn


class MaterializedDataConstructor(BaseDataConstructor):
  """Materialize a table of negative examples for fast negative generation.

  This class creates a table (num_users x num_items) containing all of the
  negative examples for each user. This table is conceptually ragged; that is to
  say the items dimension will have elements at the end which are not used equal
  to the number of positive elements for a given user. For instance:

  num_users = 3
  num_items = 5
  positives = [[1, 3], [0], [1, 2, 3, 4]]

  will generate a negative table:
  [
    [0         2         4         int32max  int32max],
    [1         2         3         4         int32max],
    [0         int32max  int32max  int32max  int32max],
  ]

  and a vector of per-user negative counts, which in this case would be:
    [3, 4, 1]

  When sampling negatives, integers are (nearly) uniformly selected from the
  range [0, per_user_neg_count[user]) which gives a column_index, at which
  point the negative can be selected as:
    negative_table[user, column_index]

  This technique will not scale; however MovieLens is small enough that even
  a pre-compute which is quadratic in problem size will still fit in memory. A
  more scalable lookup method is in the works.
  """
  def __init__(self, *args, **kwargs):
    super(MaterializedDataConstructor, self).__init__(*args, **kwargs)
    self._negative_table = None
    self._per_user_neg_count = None

  def construct_lookup_variables(self):
    # Materialize negatives for fast lookup sampling.
    start_time = timeit.default_timer()
    inner_bounds = np.argwhere(self._train_pos_users[1:] -
                               self._train_pos_users[:-1])[:, 0] + 1
    upper_bound = self._train_pos_users.shape[0]
    index_bounds = [0] + inner_bounds.tolist() + [upper_bound]
    self._negative_table = np.zeros(shape=(self._num_users, self._num_items),
                                    dtype=rconst.ITEM_DTYPE)

    # Set the table to the max value to make sure the embedding lookup will fail
    # if we go out of bounds, rather than just overloading item zero.
    self._negative_table += np.iinfo(rconst.ITEM_DTYPE).max
    assert self._num_items < np.iinfo(rconst.ITEM_DTYPE).max

    # Reuse arange during generation. np.delete will make a copy.
    full_set = np.arange(self._num_items, dtype=rconst.ITEM_DTYPE)

    self._per_user_neg_count = np.zeros(
        shape=(self._num_users,), dtype=np.int32)

    # Threading does not improve this loop. For some reason, the np.delete
    # call does not parallelize well. Multiprocessing incurs too much
    # serialization overhead to be worthwhile.
    for i in range(self._num_users):
      positives = self._train_pos_items[index_bounds[i]:index_bounds[i+1]]
      negatives = np.delete(full_set, positives)
      self._per_user_neg_count[i] = self._num_items - positives.shape[0]
      self._negative_table[i, :self._per_user_neg_count[i]] = negatives

    tf.logging.info("Negative sample table built. Time: {:.1f} seconds".format(
        timeit.default_timer() - start_time))

  def lookup_negative_items(self, negative_users, **kwargs):
    negative_item_choice = stat_utils.very_slightly_biased_randint(
        self._per_user_neg_count[negative_users])
    return self._negative_table[negative_users, negative_item_choice]
