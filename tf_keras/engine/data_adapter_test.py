# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
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
"""DataAdapter tests."""

import math

import numpy as np
import tensorflow.compat.v2 as tf
from absl.testing import parameterized

import tf_keras as keras
from tf_keras.engine import data_adapter
from tf_keras.testing_infra import test_combinations
from tf_keras.testing_infra import test_utils
from tf_keras.utils import data_utils

# isort: off
from tensorflow.python.eager import context


class DummyArrayLike:
    """Dummy array-like object."""

    def __init__(self, data):
        self.data = data

    def __len__(self):
        return len(self.data)

    def __getitem__(self, key):
        return self.data[key]

    @property
    def shape(self):
        return self.data.shape

    @property
    def dtype(self):
        return self.data.dtype


def fail_on_convert(x, **kwargs):
    _ = x
    _ = kwargs
    raise TypeError("Cannot convert DummyArrayLike to a tensor")


tf.register_tensor_conversion_function(DummyArrayLike, fail_on_convert)


class DataAdapterTestBase(test_combinations.TestCase):
    def setUp(self):
        super().setUp()
        self.batch_size = 5
        self.numpy_input = np.zeros((50, 10))
        self.numpy_target = np.ones(50)
        self.tensor_input = tf.constant(2.0, shape=(50, 10))
        self.tensor_target = tf.ones((50,))
        self.arraylike_input = DummyArrayLike(self.numpy_input)
        self.arraylike_target = DummyArrayLike(self.numpy_target)
        self.dataset_input = (
            tf.data.Dataset.from_tensor_slices(
                (self.numpy_input, self.numpy_target)
            )
            .shuffle(50)
            .batch(self.batch_size)
        )

        def generator():
            while True:
                yield (
                    np.zeros((self.batch_size, 10)),
                    np.ones(self.batch_size),
                )

        self.generator_input = generator()
        self.iterator_input = data_utils.threadsafe_generator(generator)()
        self.sequence_input = TestSequence(
            batch_size=self.batch_size, feature_shape=10
        )
        self.text_input = [["abc"]]
        self.bytes_input = [[b"abc"]]
        self.model = keras.models.Sequential(
            [keras.layers.Dense(8, input_shape=(10,), activation="softmax")]
        )


class TestSequence(data_utils.Sequence):
    def __init__(self, batch_size, feature_shape):
        self.batch_size = batch_size
        self.feature_shape = feature_shape

    def __getitem__(self, item):
        return (
            np.zeros((self.batch_size, self.feature_shape)),
            np.ones((self.batch_size,)),
        )

    def __len__(self):
        return 10


class TestSparseSequence(TestSequence):
    def __getitem__(self, item):
        indices = [
            [row, self.feature_shape - 1] for row in range(self.batch_size)
        ]
        values = [1 for row in range(self.batch_size)]
        st = tf.SparseTensor(
            indices, values, (self.batch_size, self.feature_shape)
        )
        return (st, np.ones((self.batch_size,)))


class TestRaggedSequence(TestSequence):
    def __getitem__(self, item):
        values = np.random.randint(
            0, self.feature_shape, (self.batch_size, 2)
        ).reshape(-1)
        row_lengths = np.full(self.batch_size, 2)
        rt = tf.RaggedTensor.from_row_lengths(values, row_lengths)
        return (rt, np.ones((self.batch_size,)))


class TestBatchSequence(data_utils.Sequence):
    def __init__(self, batch_size, feature_shape, epochs=2):
        """Creates a keras.utils.Sequence with increasing batch_size.

        Args:
            batch_size (Union[int, List[int]]): Can be a list containing two
                values: start and end batch_size
            feature_shape (int): Number of features in a sample
            epochs (int, optional): Number of epochs
        """
        self.batch_size = batch_size
        self.feature_shape = feature_shape

        self._epochs = epochs
        # we use `on_epoch_end` method to prepare data for the next epoch set
        # current epoch to `-1`, so that `on_epoch_end` will increase it to `0`
        self._current_epoch = -1
        # actual batch size will be set inside `on_epoch_end`
        self._current_batch_size = 0

        self.on_epoch_end()

    def __len__(self):
        """Number of batches in the Sequence.

        Returns: int
            The number of batches in the Sequence.
        """
        # data was rebalanced, so need to recalculate number of examples
        num_examples = 20
        batch_size = self._current_batch_size
        return num_examples // batch_size + int(
            num_examples % batch_size > 0
        )  # = math.ceil(num_examples / batch_size )

    def __getitem__(self, index):
        """Gets batch at position `index`.

        Arguments:
            index (int): position of the batch in the Sequence.
        Returns: Tuple[Any, Any] A batch (tuple of input data and target data).
        """
        # return input and target data, as our target data is inside the input
        # data return None for the target data
        return (
            np.zeros((self._current_batch_size, self.feature_shape)),
            np.ones((self._current_batch_size,)),
        )

    def on_epoch_end(self):
        """Updates the data after every epoch."""
        self._current_epoch += 1
        if self._current_epoch < self._epochs:
            self._current_batch_size = self._linearly_increasing_batch_size()

    def _linearly_increasing_batch_size(self):
        """Linearly increase batch size with every epoch.

        The idea comes from https://arxiv.org/abs/1711.00489.

        Returns: int
            The batch size to use in this epoch.
        """
        if not isinstance(self.batch_size, list):
            return int(self.batch_size)

        if self._epochs > 1:
            return int(
                self.batch_size[0]
                + self._current_epoch
                * (self.batch_size[1] - self.batch_size[0])
                / (self._epochs - 1)
            )
        else:
            return int(self.batch_size[0])


class TensorLikeDataAdapterTest(DataAdapterTestBase):
    def setUp(self):
        super().setUp()
        self.adapter_cls = data_adapter.TensorLikeDataAdapter

    def test_can_handle_numpy(self):
        self.assertTrue(self.adapter_cls.can_handle(self.numpy_input))
        self.assertTrue(
            self.adapter_cls.can_handle(self.numpy_input, self.numpy_target)
        )

        self.assertFalse(self.adapter_cls.can_handle(self.dataset_input))
        self.assertFalse(self.adapter_cls.can_handle(self.generator_input))
        self.assertFalse(self.adapter_cls.can_handle(self.sequence_input))
        self.assertFalse(self.adapter_cls.can_handle(self.text_input))
        self.assertFalse(self.adapter_cls.can_handle(self.bytes_input))

    def test_size_numpy(self):
        adapter = self.adapter_cls(
            self.numpy_input, self.numpy_target, batch_size=5
        )
        self.assertEqual(adapter.get_size(), 10)
        self.assertFalse(adapter.has_partial_batch())

    def test_batch_size_numpy(self):
        adapter = self.adapter_cls(
            self.numpy_input, self.numpy_target, batch_size=5
        )
        self.assertEqual(adapter.batch_size(), 5)

    def test_partial_batch_numpy(self):
        adapter = self.adapter_cls(
            self.numpy_input, self.numpy_target, batch_size=4
        )
        self.assertEqual(adapter.get_size(), 13)  # 50/4
        self.assertTrue(adapter.has_partial_batch())
        self.assertEqual(adapter.partial_batch_size(), 2)

    def test_epochs(self):
        num_epochs = 3
        adapter = self.adapter_cls(
            self.numpy_input, self.numpy_target, batch_size=5, epochs=num_epochs
        )
        ds_iter = iter(adapter.get_dataset())
        num_batches_per_epoch = self.numpy_input.shape[0] // 5
        for _ in range(num_batches_per_epoch * num_epochs):
            next(ds_iter)
        with self.assertRaises(StopIteration):
            next(ds_iter)

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training_numpy(self):
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(self.numpy_input, self.numpy_target, batch_size=5)

    def test_can_handle_pandas(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("Skipping test because pandas is not installed.")
        self.assertTrue(
            self.adapter_cls.can_handle(pd.DataFrame(self.numpy_input))
        )
        self.assertTrue(
            self.adapter_cls.can_handle(pd.DataFrame(self.numpy_input)[0])
        )
        self.assertTrue(
            self.adapter_cls.can_handle(
                pd.DataFrame(self.numpy_input),
                pd.DataFrame(self.numpy_input)[0],
            )
        )

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training_pandas(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("Skipping test because pandas is not installed.")
        input_a = keras.Input(shape=(3,), name="input_a")
        input_b = keras.Input(shape=(3,), name="input_b")
        input_c = keras.Input(shape=(1,), name="input_b")

        x = keras.layers.Dense(4, name="dense_1")(input_a)
        y = keras.layers.Dense(3, name="dense_2")(input_b)
        z = keras.layers.Dense(1, name="dense_3")(input_c)

        model_1 = keras.Model(inputs=input_a, outputs=x)
        model_2 = keras.Model(inputs=[input_a, input_b], outputs=[x, y])
        model_3 = keras.Model(inputs=input_c, outputs=z)

        model_1.compile(optimizer="rmsprop", loss="mse")
        model_2.compile(optimizer="rmsprop", loss="mse")
        model_3.compile(optimizer="rmsprop", loss="mse")

        input_a_np = np.random.random((10, 3))
        input_b_np = np.random.random((10, 3))
        input_a_df = pd.DataFrame(input_a_np)
        input_b_df = pd.DataFrame(input_b_np)

        output_a_df = pd.DataFrame(np.random.random((10, 4)))
        output_b_df = pd.DataFrame(np.random.random((10, 3)))
        output_c_series = pd.DataFrame(np.random.random((10, 4)))[0]

        model_1.fit(input_a_df, output_a_df)
        model_2.fit([input_a_df, input_b_df], [output_a_df, output_b_df])
        model_3.fit(input_a_df[[0]], output_c_series)
        model_1.fit([input_a_df], [output_a_df])
        model_1.fit({"input_a": input_a_df}, output_a_df)
        model_2.fit(
            {"input_a": input_a_df, "input_b": input_b_df},
            [output_a_df, output_b_df],
        )

        model_1.evaluate(input_a_df, output_a_df)
        model_2.evaluate([input_a_df, input_b_df], [output_a_df, output_b_df])
        model_3.evaluate(input_a_df[[0]], output_c_series)
        model_1.evaluate([input_a_df], [output_a_df])
        model_1.evaluate({"input_a": input_a_df}, output_a_df)
        model_2.evaluate(
            {"input_a": input_a_df, "input_b": input_b_df},
            [output_a_df, output_b_df],
        )

        # Verify predicting on pandas vs numpy returns the same result
        predict_1_pandas = model_1.predict(input_a_df)
        predict_2_pandas = model_2.predict([input_a_df, input_b_df])
        predict_3_pandas = model_3.predict(input_a_df[[0]])
        predict_3_pandas_batch = model_3.predict_on_batch(input_a_df[0])

        predict_1_numpy = model_1.predict(input_a_np)
        predict_2_numpy = model_2.predict([input_a_np, input_b_np])
        predict_3_numpy = model_3.predict(np.asarray(input_a_df[0]))

        self.assertAllClose(predict_1_numpy, predict_1_pandas)
        self.assertAllClose(predict_2_numpy, predict_2_pandas)
        self.assertAllClose(predict_3_numpy, predict_3_pandas_batch)
        self.assertAllClose(predict_3_numpy, predict_3_pandas)

        # Extra ways to pass in dataframes
        model_1.predict([input_a_df])
        model_1.predict({"input_a": input_a_df})
        model_2.predict({"input_a": input_a_df, "input_b": input_b_df})

    def test_can_handle(self):
        self.assertTrue(self.adapter_cls.can_handle(self.tensor_input))
        self.assertTrue(
            self.adapter_cls.can_handle(self.tensor_input, self.tensor_target)
        )

        self.assertFalse(self.adapter_cls.can_handle(self.arraylike_input))
        self.assertFalse(
            self.adapter_cls.can_handle(
                self.arraylike_input, self.arraylike_target
            )
        )
        self.assertFalse(self.adapter_cls.can_handle(self.dataset_input))
        self.assertFalse(self.adapter_cls.can_handle(self.generator_input))
        self.assertFalse(self.adapter_cls.can_handle(self.sequence_input))
        self.assertFalse(self.adapter_cls.can_handle(self.text_input))
        self.assertFalse(self.adapter_cls.can_handle(self.bytes_input))

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training(self):
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(self.tensor_input, self.tensor_target, batch_size=5)

    def test_size(self):
        adapter = self.adapter_cls(
            self.tensor_input, self.tensor_target, batch_size=5
        )
        self.assertEqual(adapter.get_size(), 10)
        self.assertFalse(adapter.has_partial_batch())

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_shuffle_correctness(self):
        num_samples = 100
        batch_size = 32
        x = np.arange(num_samples)
        np.random.seed(99)
        adapter = self.adapter_cls(
            x, y=None, batch_size=batch_size, shuffle=True, epochs=2
        )

        def _get_epoch(ds_iter):
            ds_data = []
            for _ in range(int(math.ceil(num_samples / batch_size))):
                ds_data.append(next(ds_iter).numpy())
            return np.concatenate(ds_data)

        ds_iter = iter(adapter.get_dataset())

        # First epoch.
        epoch_data = _get_epoch(ds_iter)
        # Check that shuffling occurred.
        self.assertNotAllClose(x, epoch_data)
        # Check that each elements appears, and only once.
        self.assertAllClose(x, np.sort(epoch_data))

        # Second epoch.
        second_epoch_data = _get_epoch(ds_iter)
        # Check that shuffling occurred.
        self.assertNotAllClose(x, second_epoch_data)
        # Check that shuffling is different across epochs.
        self.assertNotAllClose(epoch_data, second_epoch_data)
        # Check that each elements appears, and only once.
        self.assertAllClose(x, np.sort(second_epoch_data))

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_batch_shuffle_correctness(self):
        num_samples = 100
        batch_size = 6
        x = np.arange(num_samples)
        np.random.seed(99)
        adapter = self.adapter_cls(
            x, y=None, batch_size=batch_size, shuffle="batch", epochs=2
        )

        def _get_epoch_batches(ds_iter):
            ds_data = []
            for _ in range(int(math.ceil(num_samples / batch_size))):
                ds_data.append(next(ds_iter)[0].numpy())
            return ds_data

        ds_iter = iter(adapter.get_dataset())

        # First epoch.
        epoch_batch_data = _get_epoch_batches(ds_iter)
        epoch_data = np.concatenate(epoch_batch_data)

        def _verify_batch(batch):
            # Verify that a batch contains only contiguous data, and that it has
            # been shuffled.
            shuffled_batch = np.sort(batch)
            self.assertNotAllClose(batch, shuffled_batch)
            for i in range(1, len(batch)):
                self.assertEqual(shuffled_batch[i - 1] + 1, shuffled_batch[i])

        # Assert that the data within each batch remains contiguous
        for batch in epoch_batch_data:
            _verify_batch(batch)

        # Check that individual batches are unshuffled
        # Check that shuffling occurred.
        self.assertNotAllClose(x, epoch_data)
        # Check that each elements appears, and only once.
        self.assertAllClose(x, np.sort(epoch_data))

        # Second epoch.
        second_epoch_batch_data = _get_epoch_batches(ds_iter)
        second_epoch_data = np.concatenate(second_epoch_batch_data)

        # Assert that the data within each batch remains contiguous
        for batch in second_epoch_batch_data:
            _verify_batch(batch)

        # Check that shuffling occurred.
        self.assertNotAllClose(x, second_epoch_data)
        # Check that shuffling is different across epochs.
        self.assertNotAllClose(epoch_data, second_epoch_data)
        # Check that each elements appears, and only once.
        self.assertAllClose(x, np.sort(second_epoch_data))

    @parameterized.named_parameters(
        ("batch_size_5", 5, None, 5),
        (
            "batch_size_50",
            50,
            4,
            50,
        ),  # Sanity check: batch_size takes precedence
        ("steps_1", None, 1, 50),
        ("steps_4", None, 4, 13),
    )
    def test_batch_size(self, batch_size_in, steps, batch_size_out):
        adapter = self.adapter_cls(
            self.tensor_input,
            self.tensor_target,
            batch_size=batch_size_in,
            steps=steps,
        )
        self.assertEqual(adapter.batch_size(), batch_size_out)

    @parameterized.named_parameters(
        ("batch_size_5", 5, None, 10, 0),
        ("batch_size_4", 4, None, 13, 2),
        ("steps_1", None, 1, 1, 0),
        ("steps_5", None, 5, 5, 0),
        ("steps_4", None, 4, 4, 11),
    )
    def test_partial_batch(
        self, batch_size_in, steps, size, partial_batch_size
    ):
        adapter = self.adapter_cls(
            self.tensor_input,
            self.tensor_target,
            batch_size=batch_size_in,
            steps=steps,
        )
        self.assertEqual(adapter.get_size(), size)  # 50/steps
        self.assertEqual(adapter.has_partial_batch(), bool(partial_batch_size))
        self.assertEqual(
            adapter.partial_batch_size(), partial_batch_size or None
        )


class IncreasingBatchSizeAdapterTest(test_combinations.TestCase):
    def setUp(self):
        super(IncreasingBatchSizeAdapterTest, self).setUp()
        self.adapter_cls = data_adapter.KerasSequenceAdapter

        self.epochs = 2
        self.increasing_batch_size = [5, 10]
        self.sequence_input = TestBatchSequence(
            batch_size=self.increasing_batch_size,
            feature_shape=10,
            epochs=self.epochs,
        )
        self.model = keras.models.Sequential(
            [keras.layers.Dense(8, input_shape=(10,), activation="softmax")]
        )

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training_with_test_batch_sequence(self):
        """Ensures TestBatchSequence works as expected."""
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )

        # Check state before fit()
        self.assertEqual(self.sequence_input._current_epoch, 0)
        self.assertEqual(self.sequence_input._current_batch_size, 5)

        # Execute fit()
        self.model.fit(self.sequence_input, epochs=self.epochs)

        # Check state after fit()
        self.assertEqual(self.sequence_input._current_epoch, 2)
        self.assertEqual(self.sequence_input._current_batch_size, 10)

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training_with_increasing_batch_size(self):
        """Ensures data_adapters DataHandler & DataAdapter work as expected."""
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.stop_training = False
        self.model.train_function = self.model.make_train_function()

        # Check state before fit()
        self.assertEqual(self.sequence_input._current_epoch, 0)
        self.assertEqual(self.sequence_input._current_batch_size, 5)
        data_handler = data_adapter.get_data_handler(
            self.sequence_input,
            epochs=self.epochs,
            model=self.model,
        )
        self.assertEqual(
            data_handler.inferred_steps, 4
        )  # 20 samples / 5 bs = 4

        # Execute fit()-loop
        for epoch, iterator in data_handler.enumerate_epochs():
            self.model.reset_metrics()
            with data_handler.catch_stop_iteration():
                for step in data_handler.steps():
                    with tf.profiler.experimental.Trace(
                        "train",
                        epoch_num=epoch,
                        step_num=step,
                        batch_size=self.sequence_input._current_batch_size,
                        _r=1,
                    ):
                        if data_handler.should_sync:
                            context.async_wait()
                        if self.model.stop_training:
                            break

        # Check state after fit()
        self.assertEqual(
            data_handler.inferred_steps, 2
        )  # 20 samples / 10 bs = 2


class GenericArrayLikeDataAdapterTest(DataAdapterTestBase):
    def setUp(self):
        super().setUp()
        self.adapter_cls = data_adapter.GenericArrayLikeDataAdapter

    def test_can_handle_some_numpy(self):
        self.assertTrue(self.adapter_cls.can_handle(self.arraylike_input))
        self.assertTrue(
            self.adapter_cls.can_handle(
                self.arraylike_input, self.arraylike_target
            )
        )

        # Because adapters are mutually exclusive, don't handle cases
        # where all the data is numpy or an eagertensor
        self.assertFalse(self.adapter_cls.can_handle(self.numpy_input))
        self.assertFalse(
            self.adapter_cls.can_handle(self.numpy_input, self.numpy_target)
        )
        self.assertFalse(self.adapter_cls.can_handle(self.tensor_input))
        self.assertFalse(
            self.adapter_cls.can_handle(self.tensor_input, self.tensor_target)
        )

        # But do handle mixes that include generic arraylike data
        self.assertTrue(
            self.adapter_cls.can_handle(self.numpy_input, self.arraylike_target)
        )
        self.assertTrue(
            self.adapter_cls.can_handle(self.arraylike_input, self.numpy_target)
        )
        self.assertTrue(
            self.adapter_cls.can_handle(
                self.arraylike_input, self.tensor_target
            )
        )
        self.assertTrue(
            self.adapter_cls.can_handle(
                self.tensor_input, self.arraylike_target
            )
        )

        self.assertFalse(self.adapter_cls.can_handle(self.dataset_input))
        self.assertFalse(self.adapter_cls.can_handle(self.generator_input))
        self.assertFalse(self.adapter_cls.can_handle(self.sequence_input))
        self.assertFalse(self.adapter_cls.can_handle(self.text_input))
        self.assertFalse(self.adapter_cls.can_handle(self.bytes_input))

    def test_size(self):
        adapter = self.adapter_cls(
            self.arraylike_input, self.arraylike_target, batch_size=5
        )
        self.assertEqual(adapter.get_size(), 10)
        self.assertFalse(adapter.has_partial_batch())

    def test_epochs(self):
        num_epochs = 3
        adapter = self.adapter_cls(
            self.arraylike_input,
            self.numpy_target,
            batch_size=5,
            epochs=num_epochs,
        )
        ds_iter = iter(adapter.get_dataset())
        num_batches_per_epoch = self.numpy_input.shape[0] // 5
        for _ in range(num_batches_per_epoch * num_epochs):
            next(ds_iter)
        with self.assertRaises(StopIteration):
            next(ds_iter)

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training(self):
        # First verify that DummyArrayLike can't be converted to a Tensor
        with self.assertRaises(TypeError):
            tf.convert_to_tensor(self.arraylike_input)

        # Then train on the array like.
        # It should not be converted to a tensor directly (which would force it
        # into memory), only the sliced data should be converted.
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(
            self.arraylike_input, self.arraylike_target, batch_size=5
        )
        self.model.fit(
            self.arraylike_input,
            self.arraylike_target,
            shuffle=True,
            batch_size=5,
        )
        self.model.fit(
            self.arraylike_input,
            self.arraylike_target,
            shuffle="batch",
            batch_size=5,
        )
        self.model.evaluate(
            self.arraylike_input, self.arraylike_target, batch_size=5
        )
        self.model.predict(self.arraylike_input, batch_size=5)

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training_numpy_target(self):
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(self.arraylike_input, self.numpy_target, batch_size=5)
        self.model.fit(
            self.arraylike_input, self.numpy_target, shuffle=True, batch_size=5
        )
        self.model.fit(
            self.arraylike_input,
            self.numpy_target,
            shuffle="batch",
            batch_size=5,
        )
        self.model.evaluate(
            self.arraylike_input, self.numpy_target, batch_size=5
        )

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training_tensor_target(self):
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(self.arraylike_input, self.tensor_target, batch_size=5)
        self.model.fit(
            self.arraylike_input, self.tensor_target, shuffle=True, batch_size=5
        )
        self.model.fit(
            self.arraylike_input,
            self.tensor_target,
            shuffle="batch",
            batch_size=5,
        )
        self.model.evaluate(
            self.arraylike_input, self.tensor_target, batch_size=5
        )

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_shuffle_correctness(self):
        num_samples = 100
        batch_size = 32
        x = DummyArrayLike(np.arange(num_samples))
        np.random.seed(99)
        adapter = self.adapter_cls(
            x, y=None, batch_size=batch_size, shuffle=True, epochs=2
        )

        def _get_epoch(ds_iter):
            ds_data = []
            for _ in range(int(math.ceil(num_samples / batch_size))):
                ds_data.append(next(ds_iter).numpy())
            return np.concatenate(ds_data)

        ds_iter = iter(adapter.get_dataset())

        # First epoch.
        epoch_data = _get_epoch(ds_iter)
        # Check that shuffling occurred.
        self.assertNotAllClose(x, epoch_data)
        # Check that each elements appears, and only once.
        self.assertAllClose(x, np.sort(epoch_data))

        # Second epoch.
        second_epoch_data = _get_epoch(ds_iter)
        # Check that shuffling occurred.
        self.assertNotAllClose(x, second_epoch_data)
        # Check that shuffling is different across epochs.
        self.assertNotAllClose(epoch_data, second_epoch_data)
        # Check that each elements appears, and only once.
        self.assertAllClose(x, np.sort(second_epoch_data))

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_batch_shuffle_correctness(self):
        num_samples = 100
        batch_size = 6
        x = DummyArrayLike(np.arange(num_samples))
        np.random.seed(99)
        adapter = self.adapter_cls(
            x, y=None, batch_size=batch_size, shuffle="batch", epochs=2
        )

        def _get_epoch_batches(ds_iter):
            ds_data = []
            for _ in range(int(math.ceil(num_samples / batch_size))):
                ds_data.append(next(ds_iter)[0].numpy())
            return ds_data

        ds_iter = iter(adapter.get_dataset())

        # First epoch.
        epoch_batch_data = _get_epoch_batches(ds_iter)
        epoch_data = np.concatenate(epoch_batch_data)

        def _verify_batch(batch):
            # Verify that a batch contains only contiguous data, but that it has
            # been shuffled.
            shuffled_batch = np.sort(batch)
            self.assertNotAllClose(batch, shuffled_batch)
            for i in range(1, len(batch)):
                self.assertEqual(shuffled_batch[i - 1] + 1, shuffled_batch[i])

        # Assert that the data within each batch is shuffled contiguous data
        for batch in epoch_batch_data:
            _verify_batch(batch)

        # Check that individual batches are unshuffled
        # Check that shuffling occurred.
        self.assertNotAllClose(x, epoch_data)
        # Check that each elements appears, and only once.
        self.assertAllClose(x, np.sort(epoch_data))

        # Second epoch.
        second_epoch_batch_data = _get_epoch_batches(ds_iter)
        second_epoch_data = np.concatenate(second_epoch_batch_data)

        # Assert that the data within each batch remains contiguous
        for batch in second_epoch_batch_data:
            _verify_batch(batch)

        # Check that shuffling occurred.
        self.assertNotAllClose(x, second_epoch_data)
        # Check that shuffling is different across epochs.
        self.assertNotAllClose(epoch_data, second_epoch_data)
        # Check that each elements appears, and only once.
        self.assertAllClose(x, np.sort(second_epoch_data))

    @parameterized.named_parameters(
        ("batch_size_5", 5, None, 5),
        (
            "batch_size_50",
            50,
            4,
            50,
        ),  # Sanity check: batch_size takes precedence
        ("steps_1", None, 1, 50),
        ("steps_4", None, 4, 13),
    )
    def test_batch_size(self, batch_size_in, steps, batch_size_out):
        adapter = self.adapter_cls(
            self.arraylike_input,
            self.arraylike_target,
            batch_size=batch_size_in,
            steps=steps,
        )
        self.assertEqual(adapter.batch_size(), batch_size_out)

    @parameterized.named_parameters(
        ("batch_size_5", 5, None, 10, 0),
        ("batch_size_4", 4, None, 13, 2),
        ("steps_1", None, 1, 1, 0),
        ("steps_5", None, 5, 5, 0),
        ("steps_4", None, 4, 4, 11),
    )
    def test_partial_batch(
        self, batch_size_in, steps, size, partial_batch_size
    ):
        adapter = self.adapter_cls(
            self.arraylike_input,
            self.arraylike_target,
            batch_size=batch_size_in,
            steps=steps,
        )
        self.assertEqual(adapter.get_size(), size)  # 50/steps
        self.assertEqual(adapter.has_partial_batch(), bool(partial_batch_size))
        self.assertEqual(
            adapter.partial_batch_size(), partial_batch_size or None
        )


class DatasetAdapterTest(DataAdapterTestBase):
    def setUp(self):
        super().setUp()
        self.adapter_cls = data_adapter.DatasetAdapter

    def test_can_handle(self):
        self.assertFalse(self.adapter_cls.can_handle(self.numpy_input))
        self.assertFalse(self.adapter_cls.can_handle(self.tensor_input))
        self.assertTrue(self.adapter_cls.can_handle(self.dataset_input))
        self.assertFalse(self.adapter_cls.can_handle(self.generator_input))
        self.assertFalse(self.adapter_cls.can_handle(self.sequence_input))

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training(self):
        dataset = self.adapter_cls(self.dataset_input).get_dataset()
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(dataset)

    def test_size(self):
        adapter = self.adapter_cls(self.dataset_input)
        self.assertIsNone(adapter.get_size())

    def test_batch_size(self):
        adapter = self.adapter_cls(self.dataset_input)
        self.assertIsNone(adapter.batch_size())

    def test_partial_batch(self):
        adapter = self.adapter_cls(self.dataset_input)
        self.assertFalse(adapter.has_partial_batch())
        self.assertIsNone(adapter.partial_batch_size())

    def test_invalid_targets_argument(self):
        with self.assertRaisesRegex(
            ValueError, r"`y` argument is not supported"
        ):
            self.adapter_cls(self.dataset_input, y=self.dataset_input)

    def test_invalid_sample_weights_argument(self):
        with self.assertRaisesRegex(
            ValueError, r"`sample_weight` argument is not supported"
        ):
            self.adapter_cls(
                self.dataset_input, sample_weights=self.dataset_input
            )


class GeneratorDataAdapterTest(DataAdapterTestBase):
    def setUp(self):
        super().setUp()
        self.adapter_cls = data_adapter.GeneratorDataAdapter

    def test_can_handle(self):
        self.assertFalse(self.adapter_cls.can_handle(self.numpy_input))
        self.assertFalse(self.adapter_cls.can_handle(self.tensor_input))
        self.assertFalse(self.adapter_cls.can_handle(self.dataset_input))
        self.assertTrue(self.adapter_cls.can_handle(self.generator_input))
        self.assertFalse(self.adapter_cls.can_handle(self.sequence_input))
        self.assertFalse(self.adapter_cls.can_handle(self.text_input))
        self.assertFalse(self.adapter_cls.can_handle(self.bytes_input))

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training(self):
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(self.generator_input, steps_per_epoch=10)

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    @test_utils.run_v2_only
    @data_utils.dont_use_multiprocessing_pool
    def test_with_multiprocessing_training(self):
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(
            self.iterator_input,
            workers=1,
            use_multiprocessing=True,
            max_queue_size=10,
            steps_per_epoch=10,
        )
        # Fit twice to ensure there isn't any duplication that prevent the
        # worker from starting.
        self.model.fit(
            self.iterator_input,
            workers=1,
            use_multiprocessing=True,
            max_queue_size=10,
            steps_per_epoch=10,
        )

    def test_size(self):
        adapter = self.adapter_cls(self.generator_input)
        self.assertIsNone(adapter.get_size())

    def test_batch_size(self):
        adapter = self.adapter_cls(self.generator_input)
        self.assertEqual(adapter.batch_size(), None)
        self.assertEqual(adapter.representative_batch_size(), 5)

    def test_partial_batch(self):
        adapter = self.adapter_cls(self.generator_input)
        self.assertFalse(adapter.has_partial_batch())
        self.assertIsNone(adapter.partial_batch_size())

    def test_invalid_targets_argument(self):
        with self.assertRaisesRegex(
            ValueError, r"`y` argument is not supported"
        ):
            self.adapter_cls(self.generator_input, y=self.generator_input)

    def test_invalid_sample_weights_argument(self):
        with self.assertRaisesRegex(
            ValueError, r"`sample_weight` argument is not supported"
        ):
            self.adapter_cls(
                self.generator_input, sample_weights=self.generator_input
            )

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_not_shuffled(self):
        def generator():
            for i in range(10):
                yield np.ones((1, 1)) * i

        adapter = self.adapter_cls(generator(), shuffle=True)
        for i, data in enumerate(adapter.get_dataset()):
            self.assertEqual(i, data[0].numpy().flatten())

    def test_model_without_forward_pass(self):
        class MyModel(keras.Model):
            def train_step(self, data):
                return {"loss": 0.0}

            def test_step(self, data):
                return {"loss": 0.0}

        model = MyModel()
        model.compile("rmsprop")
        model.fit(self.generator_input, steps_per_epoch=5)
        out = model.evaluate(self.generator_input, steps=5)
        self.assertEqual(out, 0)


class KerasSequenceAdapterTest(DataAdapterTestBase):
    def setUp(self):
        super().setUp()
        self.adapter_cls = data_adapter.KerasSequenceAdapter

    def test_can_handle(self):
        self.assertFalse(self.adapter_cls.can_handle(self.numpy_input))
        self.assertFalse(self.adapter_cls.can_handle(self.tensor_input))
        self.assertFalse(self.adapter_cls.can_handle(self.dataset_input))
        self.assertFalse(self.adapter_cls.can_handle(self.generator_input))
        self.assertTrue(self.adapter_cls.can_handle(self.sequence_input))
        self.assertFalse(self.adapter_cls.can_handle(self.text_input))
        self.assertFalse(self.adapter_cls.can_handle(self.bytes_input))

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    def test_training(self):
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(self.sequence_input)

    @test_combinations.run_all_keras_modes(always_skip_v1=True)
    @test_utils.run_v2_only
    @data_utils.dont_use_multiprocessing_pool
    def test_with_multiprocessing_training(self):
        self.model.compile(
            loss="sparse_categorical_crossentropy",
            optimizer="sgd",
            run_eagerly=test_utils.should_run_eagerly(),
        )
        self.model.fit(
            self.sequence_input,
            workers=1,
            use_multiprocessing=True,
            max_queue_size=10,
            steps_per_epoch=10,
        )
        # Fit twice to ensure there isn't any duplication that prevent the
        # worker from starting.
        self.model.fit(
            self.sequence_input,
            workers=1,
            use_multiprocessing=True,
            max_queue_size=10,
            steps_per_epoch=10,
        )

    def test_size(self):
        adapter = self.adapter_cls(self.sequence_input)
        self.assertEqual(adapter.get_size(), 10)

    def test_batch_size(self):
        adapter = self.adapter_cls(self.sequence_input)
        self.assertEqual(adapter.batch_size(), None)
        self.assertEqual(adapter.representative_batch_size(), 5)

    def test_partial_batch(self):
        adapter = self.adapter_cls(self.sequence_input)
        self.assertFalse(adapter.has_partial_batch())
        self.assertIsNone(adapter.partial_batch_size())

    def test_invalid_targets_argument(self):
        with self.assertRaisesRegex(
            ValueError, r"`y` argument is not supported"
        ):
            self.adapter_cls(self.sequence_input, y=self.sequence_input)

    def test_invalid_sample_weights_argument(self):
        with self.assertRaisesRegex(
            ValueError, r"`sample_weight` argument is not supported"
        ):
            self.adapter_cls(
                self.sequence_input, sample_weights=self.sequence_input
            )


class KerasSequenceAdapterSparseTest(KerasSequenceAdapterTest):
    def setUp(self):
        super().setUp()
        self.sequence_input = TestSparseSequence(self.batch_size, 10)


class KerasSequenceAdapterRaggedTest(KerasSequenceAdapterTest):
    def setUp(self):
        super().setUp()
        self.sequence_input = TestRaggedSequence(self.batch_size, 10)

        self.model = keras.models.Sequential(
            [
                keras.layers.Input(shape=(None,), ragged=True),
                keras.layers.Embedding(10, 10),
                keras.layers.Lambda(tf.reduce_mean, arguments=dict(axis=1)),
                keras.layers.Dense(8, input_shape=(10,), activation="relu"),
            ]
        )


class DataHandlerTest(test_combinations.TestCase):
    def test_finite_dataset_with_steps_per_epoch(self):
        data = tf.data.Dataset.from_tensor_slices([0, 1, 2, 3]).batch(1)
        # User can choose to only partially consume `Dataset`.
        data_handler = data_adapter.DataHandler(
            data, initial_epoch=0, epochs=2, steps_per_epoch=2
        )
        self.assertEqual(data_handler.inferred_steps, 2)
        self.assertFalse(data_handler._adapter.should_recreate_iterator())
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator).numpy())
            returned_data.append(epoch_data)
        self.assertEqual(returned_data, [[0, 1], [2, 3]])

    def test_finite_dataset_without_steps_per_epoch(self):
        data = tf.data.Dataset.from_tensor_slices([0, 1, 2]).batch(1)
        data_handler = data_adapter.DataHandler(data, initial_epoch=0, epochs=2)
        self.assertEqual(data_handler.inferred_steps, 3)
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator).numpy())
            returned_data.append(epoch_data)
        self.assertEqual(returned_data, [[0, 1, 2], [0, 1, 2]])

    def test_finite_dataset_with_steps_per_epoch_exact_size(self):
        data = tf.data.Dataset.from_tensor_slices([0, 1, 2, 3]).batch(1)
        # If user specifies exact size of `Dataset` as `steps_per_epoch`,
        # create a new iterator each epoch.
        data_handler = data_adapter.DataHandler(
            data, initial_epoch=0, epochs=2, steps_per_epoch=4
        )
        self.assertTrue(data_handler._adapter.should_recreate_iterator())
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator).numpy())
            returned_data.append(epoch_data)
        self.assertEqual(returned_data, [[0, 1, 2, 3], [0, 1, 2, 3]])

    def test_infinite_dataset_with_steps_per_epoch(self):
        data = tf.data.Dataset.from_tensor_slices([0, 1, 2]).batch(1).repeat()
        data_handler = data_adapter.DataHandler(
            data, initial_epoch=0, epochs=2, steps_per_epoch=3
        )
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator).numpy())
            returned_data.append(epoch_data)
        self.assertEqual(returned_data, [[0, 1, 2], [0, 1, 2]])

    def test_unknown_cardinality_dataset_with_steps_per_epoch(self):
        ds = tf.data.Dataset.from_tensor_slices([0, 1, 2, 3, 4, 5, 6])
        filtered_ds = ds.filter(lambda x: x < 4)
        self.assertEqual(
            filtered_ds.cardinality().numpy(),
            tf.data.UNKNOWN_CARDINALITY,
        )

        # User can choose to only partially consume `Dataset`.
        data_handler = data_adapter.DataHandler(
            filtered_ds, initial_epoch=0, epochs=2, steps_per_epoch=2
        )
        self.assertFalse(data_handler._adapter.should_recreate_iterator())
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(returned_data)
        self.assertEqual(returned_data, [[0, 1], [2, 3]])
        self.assertEqual(data_handler.inferred_steps, 2)

    def test_unknown_cardinality_dataset_without_steps_per_epoch(self):
        ds = tf.data.Dataset.from_tensor_slices([0, 1, 2, 3, 4, 5, 6])
        filtered_ds = ds.filter(lambda x: x < 4)
        self.assertEqual(
            filtered_ds.cardinality().numpy(),
            tf.data.UNKNOWN_CARDINALITY,
        )

        data_handler = data_adapter.DataHandler(
            filtered_ds, initial_epoch=0, epochs=2
        )
        self.assertEqual(data_handler.inferred_steps, None)
        self.assertTrue(data_handler._adapter.should_recreate_iterator())
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            with data_handler.catch_stop_iteration():
                for _ in data_handler.steps():
                    epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(returned_data)
        self.assertEqual(returned_data, [[0, 1, 2, 3], [0, 1, 2, 3]])
        self.assertEqual(data_handler.inferred_steps, 4)

    def test_insufficient_data(self):
        ds = tf.data.Dataset.from_tensor_slices([0, 1])
        ds = ds.filter(lambda *args, **kwargs: True)
        data_handler = data_adapter.DataHandler(
            ds, initial_epoch=0, epochs=2, steps_per_epoch=3
        )
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                with data_handler.catch_stop_iteration():
                    epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(returned_data)
        self.assertTrue(data_handler._insufficient_data)
        self.assertEqual(returned_data, [[0, 1]])

    def test_numpy(self):
        x = np.array([0, 1, 2])
        y = np.array([0, 2, 4])
        sw = np.array([0, 4, 8])
        data_handler = data_adapter.DataHandler(
            x=x, y=y, sample_weight=sw, batch_size=1, epochs=2
        )
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(returned_data)
        self.assertEqual(
            returned_data,
            [
                [(0, 0, 0), (1, 2, 4), (2, 4, 8)],
                [(0, 0, 0), (1, 2, 4), (2, 4, 8)],
            ],
        )

    def test_generator(self):
        def generator():
            for _ in range(2):
                for step in range(3):
                    yield (tf.convert_to_tensor([step]),)

        data_handler = data_adapter.DataHandler(
            generator(), epochs=2, steps_per_epoch=3
        )
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(returned_data)
        self.assertEqual(
            returned_data, [[([0],), ([1],), ([2],)], [([0],), ([1],), ([2],)]]
        )

    def test_composite_tensor(self):
        st = tf.SparseTensor(
            indices=[[0, 0], [1, 0], [2, 0]],
            values=[0, 1, 2],
            dense_shape=[3, 1],
        )
        data_handler = data_adapter.DataHandler(st, epochs=2, steps_per_epoch=3)
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(
            tf.nest.map_structure(tf.sparse.to_dense, returned_data)
        )
        self.assertEqual(
            returned_data, [[([0],), ([1],), ([2],)], [([0],), ([1],), ([2],)]]
        )

    def test_iterator(self):
        def generator():
            for _ in range(2):
                for step in range(3):
                    yield (tf.convert_to_tensor([step]),)

        it = iter(
            tf.data.Dataset.from_generator(generator, output_types=("float32",))
        )
        data_handler = data_adapter.DataHandler(it, epochs=2, steps_per_epoch=3)
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(returned_data)
        self.assertEqual(
            returned_data, [[([0],), ([1],), ([2],)], [([0],), ([1],), ([2],)]]
        )

    def test_list_of_scalars(self):
        data_handler = data_adapter.DataHandler(
            [[0], [1], [2]], epochs=2, steps_per_epoch=3
        )
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(returned_data)
        self.assertEqual(
            returned_data, [[([0],), ([1],), ([2],)], [([0],), ([1],), ([2],)]]
        )

    def test_class_weight_user_errors(self):
        with self.assertRaisesRegex(ValueError, "to be a dict with keys"):
            data_adapter.DataHandler(
                x=[[0], [1], [2]],
                y=[[2], [1], [0]],
                batch_size=1,
                sample_weight=[[1.0], [2.0], [4.0]],
                class_weight={0: 0.5, 1: 1.0, 3: 1.5},  # Skips class `2`.
            )

        with self.assertRaisesRegex(ValueError, "with a single output"):
            data_adapter.DataHandler(
                x=np.ones((10, 1)),
                y=[np.ones((10, 1)), np.zeros((10, 1))],
                batch_size=2,
                class_weight={0: 0.5, 1: 1.0, 2: 1.5},
            )

    @parameterized.named_parameters(("one_hot", True), ("sparse", False))
    def test_class_weights_applied(self, one_hot):
        num_channels = 3
        num_classes = 5
        batch_size = 2
        image_width = 8

        input_shape = (batch_size, image_width, image_width, num_channels)
        output_shape = (batch_size, image_width, image_width)

        x = tf.random.uniform(input_shape)
        sparse_y = tf.random.uniform(
            output_shape, maxval=num_classes, dtype=tf.int32
        )

        if one_hot:
            y = tf.one_hot(sparse_y, num_classes)
        else:
            y = tf.expand_dims(sparse_y, axis=-1)

        # Class weight is equal to class number + 1
        class_weight = dict([(x, x + 1) for x in range(num_classes)])

        sample_weight = np.array([1, 2])

        data_handler = data_adapter.DataHandler(
            x=x,
            y=y,
            class_weight=class_weight,
            sample_weight=sample_weight,
            batch_size=batch_size,
            epochs=1,
        )
        returned_data = []
        for _, iterator in data_handler.enumerate_epochs():
            epoch_data = []
            for _ in data_handler.steps():
                epoch_data.append(next(iterator))
            returned_data.append(epoch_data)
        returned_data = self.evaluate(returned_data)

        # We had only 1 batch and 1 epoch, so we extract x, y, sample_weight
        result_x, result_y, result_sample_weight = returned_data[0][0]
        self.assertAllEqual(x, result_x)
        self.assertAllEqual(y, result_y)

        # Because class weight = class + 1, resulting class weight = y + 1
        # Sample weight is 1 for the first sample, 2 for the second,
        # so we double the expected sample weight for the second sample.
        self.assertAllEqual(sparse_y[0] + 1, result_sample_weight[0])
        self.assertAllEqual(2 * (sparse_y[1] + 1), result_sample_weight[1])

    @parameterized.named_parameters(("numpy", True), ("dataset", False))
    def test_single_x_input_no_tuple_wrapping(self, use_numpy):
        x = np.ones((10, 1))

        if use_numpy:
            batch_size = 2
        else:
            x = tf.data.Dataset.from_tensor_slices(x).batch(2)
            batch_size = None

        data_handler = data_adapter.DataHandler(x, batch_size=batch_size)
        for _, iterator in data_handler.enumerate_epochs():
            for _ in data_handler.steps():
                # Check that single x input is not wrapped in a tuple.
                self.assertIsInstance(next(iterator), tf.Tensor)

    def test_error_if_zero_steps_per_epoch(self):
        data = tf.data.Dataset.from_tensor_slices([0, 1, 2, 3]).batch(1)

        with self.assertRaisesRegex(
            ValueError,
            "steps_per_epoch must be positive, None or -1. Received 0.",
        ):
            data_adapter.DataHandler(
                data, initial_epoch=0, epochs=2, steps_per_epoch=0
            )

    def test_error_if_empty_array_input_data(self):
        x = np.array([[0, 0], [0, 1], [1, 0], [1, 1]])
        y = np.array([0, 1, 1, 0])
        idx = []

        with self.assertRaisesWithLiteralMatch(
            ValueError,
            "Expected input data to be non-empty.",
        ):
            data_adapter.DataHandler(x[idx], y[idx])

    def test_error_if_empty_dataset_input_data(self):
        data = tf.data.Dataset.from_tensor_slices([]).batch(1)

        with self.assertRaisesWithLiteralMatch(
            ValueError,
            "Expected input data to be non-empty.",
        ):
            data_adapter.DataHandler(data)


class TestValidationSplit(test_combinations.TestCase):
    @parameterized.named_parameters(("numpy_arrays", True), ("tensors", False))
    def test_validation_split_unshuffled(self, use_numpy):
        if use_numpy:
            x = np.array([0, 1, 2, 3, 4])
            y = np.array([0, 2, 4, 6, 8])
            sw = np.array([0, 4, 8, 12, 16])
        else:
            x = tf.convert_to_tensor([0, 1, 2, 3, 4])
            y = tf.convert_to_tensor([0, 2, 4, 6, 8])
            sw = tf.convert_to_tensor([0, 4, 8, 12, 16])

        (train_x, train_y, train_sw), (
            val_x,
            val_y,
            val_sw,
        ) = data_adapter.train_validation_split(
            (x, y, sw), validation_split=0.2
        )

        if use_numpy:
            train_x = tf.convert_to_tensor(train_x)
            train_y = tf.convert_to_tensor(train_y)
            train_sw = tf.convert_to_tensor(train_sw)
            val_x = tf.convert_to_tensor(val_x)
            val_y = tf.convert_to_tensor(val_y)
            val_sw = tf.convert_to_tensor(val_sw)

        self.assertEqual(train_x.numpy().tolist(), [0, 1, 2, 3])
        self.assertEqual(train_y.numpy().tolist(), [0, 2, 4, 6])
        self.assertEqual(train_sw.numpy().tolist(), [0, 4, 8, 12])

        self.assertEqual(val_x.numpy().tolist(), [4])
        self.assertEqual(val_y.numpy().tolist(), [8])
        self.assertEqual(val_sw.numpy().tolist(), [16])

    def test_validation_split_user_error(self):
        with self.assertRaisesRegex(
            ValueError, "is only supported for Tensors"
        ):
            data_adapter.train_validation_split(
                lambda: np.ones((10, 1)), validation_split=0.2
            )

    def test_validation_split_examples_too_few(self):
        with self.assertRaisesRegex(ValueError, "not sufficient to split it"):
            data_adapter.train_validation_split(
                np.ones((1, 10)), validation_split=0.2
            )

    def test_validation_split_none(self):
        train_sw, val_sw = data_adapter.train_validation_split(
            None, validation_split=0.2
        )
        self.assertIsNone(train_sw)
        self.assertIsNone(val_sw)

        (_, train_sw), (_, val_sw) = data_adapter.train_validation_split(
            (np.ones((10, 1)), None), validation_split=0.2
        )
        self.assertIsNone(train_sw)
        self.assertIsNone(val_sw)


class ListsOfScalarsDataAdapterTest(DataAdapterTestBase):
    def setUp(self):
        super().setUp()
        self.adapter_cls = data_adapter.ListsOfScalarsDataAdapter

    def test_can_list_inputs(self):
        self.assertTrue(self.adapter_cls.can_handle(self.text_input))
        self.assertTrue(self.adapter_cls.can_handle(self.bytes_input))

        self.assertFalse(self.adapter_cls.can_handle(self.numpy_input))
        self.assertFalse(self.adapter_cls.can_handle(self.tensor_input))
        self.assertFalse(self.adapter_cls.can_handle(self.dataset_input))
        self.assertFalse(self.adapter_cls.can_handle(self.generator_input))
        self.assertFalse(self.adapter_cls.can_handle(self.sequence_input))
        self.assertFalse(self.adapter_cls.can_handle([]))


class TestDataAdapterUtils(DataAdapterTestBase):
    def test_unpack_x_y_sample_weight_with_tuple_and_list(self):
        tuple_version = data_adapter.unpack_x_y_sample_weight(
            (self.tensor_input, self.tensor_target)
        )
        list_version = data_adapter.unpack_x_y_sample_weight(
            [self.tensor_input, self.tensor_target]
        )
        self.assertEqual(tuple_version, list_version)

    def test_unpack_pack_dict(self):
        # A dictionary can be unambiguously represented without a tuple.
        x = {"key": self.tensor_input}
        packed_x = data_adapter.pack_x_y_sample_weight(x)
        self.assertEqual(packed_x, x)
        unpacked_x, _, _ = data_adapter.unpack_x_y_sample_weight(x)
        self.assertEqual(unpacked_x, x)


if __name__ == "__main__":
    tf.compat.v1.enable_eager_execution()
    tf.test.main()
