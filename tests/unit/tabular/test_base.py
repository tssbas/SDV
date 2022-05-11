import os
from unittest.mock import ANY, MagicMock, Mock, call, patch

import pandas as pd
import pytest
import tqdm

from sdv.metadata.table import Table
from sdv.sampling import Condition
from sdv.tabular.base import COND_IDX, FIXED_RNG_SEED, TMP_FILE_NAME, BaseTabularModel
from sdv.tabular.copulagan import CopulaGAN
from sdv.tabular.copulas import GaussianCopula
from sdv.tabular.ctgan import CTGAN, TVAE
from tests.utils import DataFrameMatcher

MODELS = [
    CTGAN(epochs=1),
    TVAE(epochs=1),
    GaussianCopula(),
    CopulaGAN(epochs=1),
]


class TestBaseTabularModel:

    def test___init__metadata_object(self):
        """Test ``__init__`` passing a ``Table`` object.

        In this case, the metadata object should be copied and stored as
        ``instance.table_metadata``.

        Input:
            - table_metadata
            - field_distributions
            - default_distribution
            - categorical_transformer

        Side Effects
            - attributes are set to the right values
            - metadata is created with the right values
            - ``instance.metadata`` is different than the object provided
        """
        # Setup
        metadata_dict = {
            'name': 'test',
            'fields': {
                'a_field': {
                    'type': 'categorical'
                },
            },
            'model_kwargs': {
                'GaussianCopula': {
                    'field_distributions': {
                        'a_field': 'gaussian',
                    },
                    'categorical_transformer': 'categorical_fuzzy',
                }
            }
        }
        table_metadata = Table.from_dict(metadata_dict)

        # Run
        gc = GaussianCopula(
            default_distribution='bounded',
            table_metadata=table_metadata,
        )

        # Assert
        assert gc._metadata.get_fields() == table_metadata.get_fields()
        kwargs = gc._metadata.get_model_kwargs('GaussianCopula')
        provided_kwargs = table_metadata.get_model_kwargs('GaussianCopula')
        assert kwargs['field_distributions'] == provided_kwargs['field_distributions']
        assert kwargs['categorical_transformer'] == provided_kwargs['categorical_transformer']
        assert 'default_distribution' not in provided_kwargs
        assert gc._metadata != table_metadata

    def test__sample_with_conditions_no_transformed_columns(self):
        """Test the ``BaseTabularModel.sample`` method with no transformed columns.

        When the transformed conditions DataFrame has no columns, expect that sample
        does not pass through any conditions when conditionally sampling.

        Setup:
            - Mock the ``_make_condition_dfs`` method to return a dataframe representing
              the expected conditions, and the ``get_fields`` method to return metadata
              fields containing the expected conditioned column.
            - Mock the ``_metadata.transform`` method to return an empty transformed
              conditions dataframe.
            - Mock the ``_conditionally_sample_rows`` method to return the expected
              sampled rows.
            - Mock the `make_ids_unique` to return the expected sampled rows.
        Input:
            - number of rows
            - one set of conditions
        Output:
            - the expected sampled rows
        Side Effects:
            - Expect ``_conditionally_sample_rows`` to be called with the given condition
              and a transformed_condition of None.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        expected = pd.DataFrame(['a', 'a', 'a'])

        condition_dataframe = pd.DataFrame({'a': ['a', 'a', 'a']})
        model._make_condition_dfs.return_value = condition_dataframe
        model._metadata.get_fields.return_value = ['a']
        model._metadata.transform.return_value = pd.DataFrame({}, index=[0, 1, 2])
        model._conditionally_sample_rows.return_value = pd.DataFrame({
            'a': ['a', 'a', 'a'],
            COND_IDX: [0, 1, 2]})
        model._metadata.make_ids_unique.return_value = expected

        # Run
        out = BaseTabularModel._sample_with_conditions(model, condition_dataframe, 100, None)

        # Asserts
        model._conditionally_sample_rows.assert_called_once_with(
            DataFrameMatcher(pd.DataFrame({COND_IDX: [0, 1, 2], 'a': ['a', 'a', 'a']})),
            {'a': 'a'},
            None,
            100,
            None,
            progress_bar=None,
            output_file_path=None,
        )
        pd.testing.assert_frame_equal(out, expected)

    def test__sample_batch_zero_valid(self):
        """Test the `BaseTabularModel._sample_batch` method with zero valid rows.

        Expect that the requested number of rows are returned, if the first `_sample_rows` call
        returns zero valid rows, and the second one returns enough valid rows.
        See https://github.com/sdv-dev/SDV/issues/285.

        Input:
            - num_rows = 5
            - condition on `column1` = 2
        Output:
            - The requested number of sampled rows (5).
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        valid_sampled_data = pd.DataFrame({
            "column1": [28, 28, 21, 1, 2],
            "column2": [37, 37, 1, 4, 5],
            "column3": [93, 93, 6, 4, 12],
        })
        model._sample_rows.side_effect = [(pd.DataFrame({}), 0), (valid_sampled_data, 5)]

        conditions = {
            'column1': 2,
            'column1': 2,
            'column1': 2,
            'column1': 2,
            'column1': 2,
        }

        # Run
        output = BaseTabularModel._sample_batch(model, num_rows=5, conditions=conditions)

        # Assert
        assert model._sample_rows.call_count == 2
        assert len(output) == 5

    @patch('sdv.tabular.base.os.path', spec=os.path)
    def test__sample_batch_output_file_path(self, path_mock):
        """Test the `BaseTabularModel._sample_batch` method with a valid output file path.

        Expect that if the output file is empty, the sampled rows are written to the file
        with the header included in the first batch write.

        Input:
            - num_rows = 4
            - output_file_path = temp file
        Output:
            - The requested number of sampled rows (4).
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        sampled_mock = MagicMock()
        sampled_mock.__len__.return_value = 4
        model._sample_rows.return_value = (sampled_mock, 4)
        output_file_path = 'test.csv'
        path_mock.getsize.return_value = 0

        # Run
        output = BaseTabularModel._sample_batch(
            model, num_rows=4, output_file_path=output_file_path)

        # Assert
        assert model._sample_rows.call_count == 1
        assert output == sampled_mock.head.return_value
        assert sampled_mock.head.return_value.tail.return_value.to_csv.called_once_with(
            call(2).tail(2).to_csv(output_file_path, index=False),
        )

    @patch('sdv.tabular.utils.tqdm.tqdm', spec=tqdm.tqdm)
    def test_sample_valid_num_rows(self, tqdm_mock):
        """Test the `BaseTabularModel.sample` method with a valid `num_rows` argument.

        Expect that the expected call to `_sample_batch` is made.

        Input:
            - num_rows = 5
        Output:
            - The requested number of sampled rows.
        Side Effect:
            - Call `_sample_batch` method with the expected number of rows.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        valid_sampled_data = pd.DataFrame({
            'column1': [28, 28, 21, 1, 2],
            'column2': [37, 37, 1, 4, 5],
            'column3': [93, 93, 6, 4, 12],
        })
        model._sample_batch.return_value = valid_sampled_data

        # Run
        output = BaseTabularModel.sample(model, 5)

        # Assert
        assert model._sample_batch.called_once_with(5)
        assert tqdm_mock.call_count == 0
        assert len(output) == 5

    def test_sample_no_num_rows(self):
        """Test the `BaseTabularModel.sample` method with no `num_rows` input.

        Expect that an error is thrown.
        """
        # Setup
        model = BaseTabularModel()

        # Run and assert
        with pytest.raises(
                TypeError,
                match=r'sample\(\) missing 1 required positional argument: \'num_rows\''):
            model.sample()

    def test_sample_num_rows_none(self):
        """Test the `BaseTabularModel.sample` method with a `num_rows` input of `None`.

        Expect that a `ValueError` is thrown.

        Input:
            - num_rows = None
        Side Effect:
            - ValueError
        """
        # Setup
        model = BaseTabularModel()
        num_rows = None

        # Run and assert
        with pytest.raises(
                ValueError,
                match=r'You must specify the number of rows to sample \(e.g. num_rows=100\)'):
            model.sample(num_rows)

    @patch('sdv.tabular.utils.tqdm.tqdm', spec=tqdm.tqdm)
    def test_sample_batch_size(self, tqdm_mock):
        """Test the `BaseTabularModel.sample` method with a valid `batch_size` argument.

        Expect that the expected calls to `_sample_batch` are made.

        Input:
            - num_rows = 10
            - batch_size = 5
        Output:
            - The requested number of sampled rows.
        Side Effect:
            - Call `_sample_batch` method twice with the expected number of rows.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        sampled_data = pd.DataFrame({
            'column1': [28, 28, 21, 1, 2],
            'column2': [37, 37, 1, 4, 5],
            'column3': [93, 93, 6, 4, 12],
        })
        model._sample_batch.side_effect = [sampled_data, sampled_data]

        # Run
        output = BaseTabularModel.sample(model, 10, batch_size=5)

        # Assert
        assert model._sample_batch.has_calls([
            call(5, batch_size_per_try=5, progress_bar=ANY, output_file_path=None),
            call(5, batch_size_per_try=5, progress_bar=ANY, output_file_path=None),
        ])
        tqdm_mock.assert_has_calls([call(total=10)])
        assert len(output) == 10

    def test__sample_batch_with_batch_size_per_try(self):
        """Test the `BaseTabularModel._sample_batch` method with `batch_size_per_try`.

        Expect that the expected calls to `_sample_rows` are made.

        Input:
            - num_rows = 10
            - batch_size_per_try = 5
        Output:
            - The requested number of sampled rows.
        Side Effect:
            - Call `_sample_rows` method twice with the expected number of rows.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        sampled_data = pd.DataFrame({
            'column1': [28, 28, 21, 1, 2],
            'column2': [37, 37, 1, 4, 5],
            'column3': [93, 93, 6, 4, 12],
        })
        model._sample_rows.side_effect = [
            (sampled_data, 5),
            (sampled_data.append(sampled_data, ignore_index=False), 10),
        ]

        # Run
        output = BaseTabularModel._sample_batch(model, num_rows=10, batch_size_per_try=5)

        # Assert
        assert model._sample_rows.has_calls([
            call(5, None, None, 0.01, DataFrameMatcher(pd.DataFrame())),
            call(5, None, None, 0.01, DataFrameMatcher(sampled_data)),
        ])
        assert len(output) == 10

    def test__sample_conditions_with_multiple_conditions(self):
        """Test the `BaseTabularModel._sample_conditions` method with multiple condtions.

        When multiple condition dataframes are returned by `_make_condition_dfs`,
        expect `_sample_with_conditions` is called for each condition dataframe.

        Input:
            - 2 conditions with different columns
        Output:
            - The expected sampled rows
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        model._validate_file_path.return_value = None

        condition_values1 = {'cola': 'a'}
        condition1 = Condition(condition_values1, num_rows=2)
        sampled1 = pd.DataFrame({'a': ['a', 'a'], 'b': [1, 2]})

        condition_values2 = {'colb': 1}
        condition2 = Condition(condition_values2, num_rows=3)
        sampled2 = pd.DataFrame({'a': ['b', 'c', 'a'], 'b': [1, 1, 1]})

        expected = pd.DataFrame({
            'a': ['a', 'a', 'b', 'c', 'a'],
            'b': [1, 2, 1, 1, 1],
        })

        model._make_condition_dfs.return_value = [
            pd.DataFrame([condition_values1] * 2),
            pd.DataFrame([condition_values2] * 3),
        ]
        model._sample_with_conditions.side_effect = [
            sampled1,
            sampled2,
        ]

        # Run
        out = BaseTabularModel._sample_conditions(
            model, [condition1, condition2], 100, None, True, None)

        # Asserts
        model._sample_with_conditions.assert_has_calls([
            call(DataFrameMatcher(pd.DataFrame([condition_values1] * 2)), 100,
                 None, ANY, None),
            call(DataFrameMatcher(pd.DataFrame([condition_values2] * 3)), 100,
                 None, ANY, None),
        ])
        pd.testing.assert_frame_equal(out, expected)

    def test__sample_conditions_no_rows(self):
        """Test `BaseTabularModel._sample_conditions` with invalid condition.

        If no valid rows are returned for any condition, expect a ValueError.

        Input:
            - condition that is impossible to satisfy
        Side Effects:
            - ValueError is thrown
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        condition = Condition(
            {'column1': 'b'},
            num_rows=5,
        )
        model._make_condition_dfs.return_value = pd.DataFrame([{'column1': 'b'}] * 5)
        model._sample_with_conditions.return_value = pd.DataFrame()

        # Run and assert
        with pytest.raises(ValueError,
                           match='Unable to sample any rows for the given conditions.'):
            BaseTabularModel._sample_conditions(model, [condition], 100, None, True, None)

    def test_sample_conditions(self):
        """Test `BaseTabularModel.sample_conditions` method.

        Expect the correct args to be passed to `_sample_conditions`.

        Input:
            - valid conditions
        Side Effects:
            - The expected `_sample_conditions` call.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        condition = Condition(
            {'column1': 'b'},
            num_rows=5,
        )

        # Run
        out = BaseTabularModel.sample_conditions(model, [condition])

        # Assert
        model._sample_conditions.assert_called_once_with([condition], 100, None, True, None)
        assert out == model._sample_conditions.return_value

    def test__conditionally_sample_rows_graceful_reject_sampling_true(self):
        """Test the `BaseTabularModel._conditionally_sample_rows` method.

        When `_sample_with_conditions` is called with `graceful_reject_sampling` as True,
        expect that there are no errors if no valid rows are generated.

        Input:
            - An impossible condition
        Returns:
            - Empty DataFrame
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        model._validate_file_path.return_value = None

        condition_values = {'cola': 'c'}
        transformed_conditions = pd.DataFrame([condition_values] * 2)
        condition = Condition(condition_values, num_rows=2)

        model._sample_batch.return_value = pd.DataFrame()

        # Run
        sampled = BaseTabularModel._conditionally_sample_rows(
            model,
            pd.DataFrame([condition_values] * 2),
            condition,
            transformed_conditions,
            graceful_reject_sampling=True,
        )

        # Assert
        assert len(sampled) == 0
        model._sample_batch.assert_called_once_with(
            2, None, None, condition, transformed_conditions, 0.01, None, None)

    def test__conditionally_sample_rows_graceful_reject_sampling_false(self):
        """Test the `BaseTabularModel._conditionally_sample_rows` method.

        When `_sample_with_conditions` is called with `graceful_reject_sampling` as False,
        expect that an error is thrown if no valid rows are generated.

        Input:
            - An impossible condition
        Side Effect:
            - A ValueError is thrown
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        model._validate_file_path.return_value = None

        condition_values = {'cola': 'c'}
        transformed_conditions = pd.DataFrame([condition_values] * 2)
        condition = Condition(condition_values, num_rows=2)

        model._sample_batch.return_value = pd.DataFrame()

        # Run and assert
        with pytest.raises(ValueError,
                           match='Unable to sample any rows for the given conditions'):
            BaseTabularModel._conditionally_sample_rows(
                model,
                pd.DataFrame([condition_values] * 2),
                condition,
                transformed_conditions,
                graceful_reject_sampling=False,
            )

        model._sample_batch.assert_called_once_with(
            2, None, None, condition, transformed_conditions, 0.01, None, None)

    def test__sample_remaining_columns(self):
        """Test the `BaseTabularModel._sample_remaining_colmns` method.

        When a valid DataFrame is given, expect `_sample_with_conditions` to be called
        with the input DataFrame.

        Input:
            - DataFrame with condition column values populated.
        Output:
            - The expected sampled rows.
        Side Effects:
            - `_sample_with_conditions` is called once.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        model._validate_file_path.return_value = None

        conditions = pd.DataFrame([{'cola': 'a'}] * 5)

        sampled = pd.DataFrame({
            'cola': ['a', 'a', 'a', 'a', 'a'],
            'colb': [1, 2, 1, 1, 1],
        })
        model._sample_with_conditions.return_value = sampled

        # Run
        out = BaseTabularModel._sample_remaining_columns(model, conditions, 100, None, True, None)

        # Asserts
        model._sample_with_conditions.assert_called_once_with(
            DataFrameMatcher(conditions), 100, None, ANY, None)
        pd.testing.assert_frame_equal(out, sampled)

    def test__sample_remaining_columns_no_rows(self):
        """Test `BaseTabularModel._sample_remaining_columns` with invalid condition.

        If no valid rows are returned for any condition, expect a ValueError.

        Input:
            - condition that is impossible to satisfy
        Side Effects:
            - ValueError is thrown
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        conditions = pd.DataFrame([{'cola': 'a'}] * 5)
        model._sample_with_conditions.return_value = pd.DataFrame()

        # Run and assert
        with pytest.raises(
            ValueError,
            match='Unable to sample any rows for the given conditions.'
        ):
            BaseTabularModel._sample_remaining_columns(model, conditions, 100, None, True, None)

    def test_sample_remaining_columns(self):
        """Test `BaseTabularModel.sample_remaining_columns` method.

        Expect the correct args to be passed to `_sample_remaining_columns`.

        Input:
            - valid DataFrame
        Side Effects:
            - The expected `_sample_remaining_columns` call.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        conditions = pd.DataFrame([{'cola': 'a'}] * 5)

        # Run
        out = BaseTabularModel.sample_remaining_columns(model, conditions)

        # Assert
        model._sample_remaining_columns.assert_called_once_with(conditions, 100, None, True, None)
        assert out == model._sample_remaining_columns.return_value

    def test__validate_conditions_with_conditions_valid_columns(self):
        """Test the `BaseTabularModel._validate_conditions` method with valid columns.

        Expect no error to be thrown.

        Input:
            - Conditions DataFrame contains only valid columns.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        metadata_mock = Mock()
        metadata_mock.get_fields.return_value = {'cola': {}}
        model._metadata = metadata_mock

        conditions = pd.DataFrame([{'cola': 'a'}] * 5)

        # Run and Assert
        BaseTabularModel._validate_conditions(model, conditions)

    def test__validate_conditions_with_conditions_invalid_column(self):
        """Test the `BaseTabularModel._validate_conditions` method with an invalid column.

        When a condition has an invalid column, expect a ValueError.

        Input:
            - Conditions DataFrame with an invalid column.
        Side Effects:
            - A ValueError is thrown.
        """
        # Setup
        model = Mock(spec_set=CTGAN)
        metadata_mock = Mock()
        metadata_mock.get_fields.return_value = {'cola': {}}
        model._metadata = metadata_mock

        conditions = pd.DataFrame([{'colb': 'a'}] * 5)

        # Run and Assert
        with pytest.raises(ValueError, match=(
                'Unexpected column name `colb`. '
                'Use a column name that was present in the original data.')):
            BaseTabularModel._validate_conditions(model, conditions)

    @patch('sdv.tabular.base.os.path')
    def test__validate_file_path(self, path_mock):
        """Test the `BaseTabularModel._validate_file_path` method.

        Expect that an error is thrown if the file path already exists.

        Input:
            - A file path that already exists.
        Side Effects:
            - An AssertionError.
        """
        # Setup
        path_mock.exists.return_value = True
        path_mock.abspath.return_value = 'path/to/file'
        model = Mock(spec_set=CTGAN)

        # Run and Assert
        with pytest.raises(AssertionError, match='path/to/file already exists'):
            BaseTabularModel._validate_file_path(model, 'file_path')

    @patch('sdv.tabular.base.os')
    def test_sample_with_default_file_path(self, os_mock):
        """Test the `BaseTabularModel.sample` method with the default file path.

        Expect that the file is removed after successfully sampling.

        Input:
            - output_file_path=None.
        Side Effects:
            - The file is removed.
        """
        # Setup
        model = Mock()
        model._validate_file_path.return_value = TMP_FILE_NAME
        model._sample_batch.return_value = pd.DataFrame({'test': [1]})
        os_mock.path.exists.return_value = True

        # Run
        BaseTabularModel.sample(model, 1, output_file_path=None)

        # Assert
        model._sample_batch.called_once_with(
            1, batch_size_per_try=1, progress_bar=ANY, output_file_path=TMP_FILE_NAME)
        os_mock.remove.called_once_with(TMP_FILE_NAME)

    @patch('sdv.tabular.base.os')
    def test_sample_with_default_file_path_error(self, os_mock):
        """Test the `BaseTabularModel.sample` method with the default file path.

        Expect that the file is not removed if there is an error with sampling.

        Input:
            - output_file_path=None.
        Side Effects:
            - ValueError is thrown.
        """
        # Setup
        model = Mock()
        model._validate_file_path.return_value = TMP_FILE_NAME
        model._sample_batch.side_effect = ValueError('test error')

        # Run
        with pytest.raises(ValueError, match='test error'):
            BaseTabularModel.sample(model, 1, output_file_path=None)

        # Assert
        model._sample_batch.called_once_with(
            1, batch_size_per_try=1, progress_bar=ANY, output_file_path=TMP_FILE_NAME)
        assert os_mock.remove.call_count == 0

    @patch('sdv.tabular.base.os')
    def test_sample_with_custom_file_path(self, os_mock):
        """Test the `BaseTabularModel.sample` method with a custom file path.

        Expect that the file is not removed if a custom file path is given.

        Input:
            - output_file_path='temp.csv'.
        Side Effects:
            - None
        """
        # Setup
        model = Mock()
        model._validate_file_path.return_value = 'temp.csv'
        model._sample_batch.return_value = pd.DataFrame({'test': [1]})

        # Run
        BaseTabularModel.sample(model, 1, output_file_path='temp.csv')

        # Assert
        model._sample_batch.called_once_with(
            1, batch_size_per_try=1, progress_bar=ANY, output_file_path='temp.csv')
        assert os_mock.remove.call_count == 0


@patch('sdv.tabular.base.Table', spec_set=Table)
def test__init__passes_correct_parameters(metadata_mock):
    """
    Tests the ``BaseTabularModel.__init__`` method.

    The method should pass the parameters to the ``Table``
    class.

    Input:
    - rounding set to an int
    - max_value set to an int
    - min_value set to an int
    Side Effects:
    - ``instance._metadata`` should receive the correct parameters
    """
    # Run
    GaussianCopula(rounding=-1, max_value=100, min_value=-50)
    CTGAN(epochs=1, rounding=-1, max_value=100, min_value=-50)
    TVAE(epochs=1, rounding=-1, max_value=100, min_value=-50)
    CopulaGAN(epochs=1, rounding=-1, max_value=100, min_value=-50)

    # Asserts
    assert len(metadata_mock.mock_calls) == 5
    expected_calls = [
        call(field_names=None, primary_key=None, field_types=None, field_transformers=None,
             anonymize_fields=None, constraints=None,
             dtype_transformers={'O': 'categorical_fuzzy'}, rounding=-1, max_value=100,
             min_value=-50),
        call(field_names=None, primary_key=None, field_types=None, field_transformers=None,
             anonymize_fields=None, constraints=None, dtype_transformers={'O': None},
             rounding=-1, max_value=100, min_value=-50),
        call(field_names=None, primary_key=None, field_types=None, field_transformers=None,
             anonymize_fields=None, constraints=None, dtype_transformers={'O': None},
             rounding=-1, max_value=100, min_value=-50),
        call(field_names=None, primary_key=None, field_types=None, field_transformers=None,
             anonymize_fields=None, constraints=None, dtype_transformers={'O': None},
             rounding=-1, max_value=100, min_value=-50)
    ]
    metadata_mock.assert_has_calls(expected_calls, any_order=True)


@pytest.mark.parametrize('model', MODELS)
def test__sample_conditions_graceful_reject_sampling(model):
    data = pd.DataFrame({
        'column1': list(range(100)),
        'column2': list(range(100)),
        'column3': list(range(100))
    })

    conditions = [
        Condition(
            {'column1': 'this is not used'},
            num_rows=5,
        )
    ]

    model._sample_batch = Mock()
    model._sample_batch.return_value = pd.DataFrame({
        'column1': [28, 28],
        'column2': [37, 37],
        'column3': [93, 93],
    })

    model.fit(data)
    output = model._sample_conditions(conditions, 100, None, True, None)
    assert len(output) == 2, 'Only expected 2 valid rows.'


@pytest.mark.parametrize('model', MODELS)
def test__sample_conditions_with_value_zero(model):
    data = pd.DataFrame({
        'column1': list(range(100)),
        'column2': list(range(100)),
        'column3': list(range(100))
    })
    data = data.astype(float)

    conditions = [
        Condition(
            {'column1': 0},
            num_rows=1,
        ),
        Condition(
            {'column1': 0.0},
            num_rows=1,
        )
    ]

    model.fit(data)
    output = model._sample_conditions(conditions, 100, None, True, None)
    assert len(output) == 2, 'Expected 2 valid rows.'


def test__sample_rows_previous_rows_appended_correctly():
    """Test the ``BaseTabularModel._sample_rows`` method.

    If ``_sample_rows`` is passed ``previous_rows``, then it
    should reset the index when appending them to the new
    sampled rows.

    Input:
    - num_rows is 5
    - previous_rows is a DataFrame of 3 existing rows.

    Output:
    - 5 sampled rows with index set to [0, 1, 2, 3, 4]
    """
    # Setup
    model = GaussianCopula()
    previous_data = pd.DataFrame({
        'column1': [1, 2, 3],
        'column2': [4, 5, 6],
        'column3': [7, 8, 9]
    })
    new_data = pd.DataFrame({
        'column1': [4, 5],
        'column2': [7, 8],
        'column3': [10, 11]
    })
    model._metadata = Mock()
    model._sample = Mock()
    model._sample.return_value = new_data
    model._metadata.reverse_transform.return_value = new_data
    model._metadata.filter_valid = lambda x: x

    # Run
    sampled, num_valid = model._sample_rows(5, previous_rows=previous_data)

    # Assert
    expected = pd.DataFrame({
        'column1': [1, 2, 3, 4, 5],
        'column2': [4, 5, 6, 7, 8],
        'column3': [7, 8, 9, 10, 11]
    })
    assert num_valid == 5
    pd.testing.assert_frame_equal(sampled, expected)


def test__sample_with_conditions_empty_transformed_conditions():
    """Test that None is passed to ``_sample_batch`` if transformed conditions are empty.

    The ``Sample`` method is expected to:
    - Return sampled data and pass None to ``sample_batch`` as the
    ``transformed_conditions``.

    Input:
    - Number of rows to sample
    - Conditions

    Output:
    - Sampled data
    """
    # Setup
    model = GaussianCopula()
    data = pd.DataFrame({
        'column1': list(range(100)),
        'column2': list(range(100)),
        'column3': list(range(100))
    })

    conditions = {
        'column1': 25
    }
    conditions_series = pd.Series([25, 25, 25, 25, 25], name='column1')
    model._sample_batch = Mock()
    sampled = pd.DataFrame({
        'column1': [28, 28],
        'column2': [37, 37],
        'column3': [93, 93],
    })
    model._sample_batch.return_value = sampled
    model.fit(data)
    model._metadata = Mock()
    model._metadata.get_fields.return_value = ['column1', 'column2', 'column3']
    model._metadata.transform.return_value = pd.DataFrame()
    model._metadata.make_ids_unique.side_effect = lambda x: x

    # Run
    output = model._sample_with_conditions(pd.DataFrame([conditions] * 5), 100, None)

    # Assert
    expected_output = pd.DataFrame({
        'column1': [28, 28],
        'column2': [37, 37],
        'column3': [93, 93],
    })
    _, args, kwargs = model._metadata.transform.mock_calls[0]
    pd.testing.assert_series_equal(args[0]['column1'], conditions_series)
    assert kwargs['on_missing_column'] == 'drop'
    model._metadata.transform.assert_called_once()
    model._sample_batch.assert_called_with(5, 100, None, conditions, None, 0.01, None, None)
    pd.testing.assert_frame_equal(output, expected_output)


def test__sample_with_conditions_transform_conditions_correctly():
    """Test that transformed conditions are batched correctly.

    The ``Sample`` method is expected to:
    - Return sampled data and call ``_sample_batch`` for every unique transformed
    condition group.

    Input:
    - Number of rows to sample
    - Conditions

    Output:
    - Sampled data
    """
    # Setup
    model = GaussianCopula()
    data = pd.DataFrame({
        'column1': list(range(100)),
        'column2': list(range(100)),
        'column3': list(range(100))
    })

    condition_values = [25, 25, 25, 30, 30]
    conditions_series = pd.Series([25, 25, 25, 30, 30], name='column1')
    model._sample_batch = Mock()
    expected_outputs = [
        pd.DataFrame({
            'column1': [25, 25, 25],
            'column2': [37, 37, 37],
            'column3': [93, 93, 93],
        }), pd.DataFrame({
            'column1': [30],
            'column2': [37],
            'column3': [93],
        }), pd.DataFrame({
            'column1': [30],
            'column2': [37],
            'column3': [93],
        })
    ]
    model._sample_batch.side_effect = expected_outputs
    model.fit(data)
    model._metadata = Mock()
    model._metadata.get_fields.return_value = ['column1', 'column2', 'column3']
    model._metadata.transform.return_value = pd.DataFrame([
        [50], [50], [50], [60], [70]
    ], columns=['transformed_column'])

    # Run
    model._sample_with_conditions(
        pd.DataFrame({'column1': condition_values}), 100, None)

    # Assert
    _, args, kwargs = model._metadata.transform.mock_calls[0]
    pd.testing.assert_series_equal(args[0]['column1'], conditions_series)
    assert kwargs['on_missing_column'] == 'drop'
    model._metadata.transform.assert_called_once()
    model._sample_batch.assert_any_call(
        3, 100, None, {'column1': 25}, {'transformed_column': 50}, 0.01, None, None,
    )
    model._sample_batch.assert_any_call(
        1, 100, None, {'column1': 30}, {'transformed_column': 60}, 0.01, None, None,
    )
    model._sample_batch.assert_any_call(
        1, 100, None, {'column1': 30}, {'transformed_column': 70}, 0.01, None, None,
    )


@pytest.mark.parametrize('model', MODELS)
def test_fit_sets_num_rows(model):
    """Test ``fit`` sets ``_num_rows`` to the length of the data passed.

    The ``fit`` method is expected to:
        - Save the length of the data passed to the ``fit`` method in the ``_num_rows`` attribute.

    Input:
        - DataFrame

    Side Effects:
        - ``_num_rows`` is set to the length of the data passed to ``fit``.
    """
    # Setup
    _N_DATA_ROWS = 100
    data = pd.DataFrame({
        'column1': list(range(_N_DATA_ROWS)),
        'column2': list(range(_N_DATA_ROWS)),
        'column3': list(range(_N_DATA_ROWS))
    })

    # Run
    model.fit(data)

    # Assert
    assert model._num_rows == _N_DATA_ROWS


@pytest.mark.parametrize('model', MODELS)
def test__make_condition_dfs_without_num_rows(model):
    """Test ``_make_condition_dfs`` works correctly when ``num_rows`` is not passed.

    The ``_make_condition_dfs`` method is expected to:
        - Return conditions as a ``DataFrame`` for one row.

    Input:
        - Conditions

    Output:
        - Conditions as ``[DataFrame]``
    """
    # Setup
    column_values = {'column2': 'M'}
    conditions = [Condition(column_values=column_values)]
    expected_conditions = pd.DataFrame([column_values])

    # Run
    result_conditions_list = model._make_condition_dfs(conditions=conditions)

    # Assert
    assert len(result_conditions_list) == 1
    result_conditions = result_conditions_list[0]
    assert isinstance(result_conditions, pd.DataFrame)
    assert len(result_conditions) == 1
    assert all(result_conditions == expected_conditions)


@pytest.mark.parametrize('model', MODELS)
def test__make_condition_dfs_specifying_num_rows(model):
    """Test ``_make_condition_dfs`` works correctly when ``num_rows`` is passed.

    The ``_make_condition_dfs`` method is expected to:
    - Return as many condition rows as specified with ``num_rows`` as a ``DataFrame``.

    Input:
        - Conditions
        - Num_rows

    Output:
        - Conditions as ``[DataFrame]``
    """
    # Setup
    _NUM_ROWS = 10
    column_values = {'column2': 'M'}
    conditions = [Condition(column_values=column_values, num_rows=_NUM_ROWS)]
    expected_conditions = pd.DataFrame([column_values] * _NUM_ROWS)

    # Run
    result_conditions_list = model._make_condition_dfs(conditions=conditions)

    # Assert
    assert len(result_conditions_list) == 1
    result_conditions = result_conditions_list[0]
    assert isinstance(result_conditions, pd.DataFrame)
    assert len(result_conditions) == _NUM_ROWS
    assert all(result_conditions == expected_conditions)


@pytest.mark.parametrize('model', MODELS)
def test__make_condition_dfs_with_multiple_conditions_same_column(model):
    """Test ``_make_condition_dfs`` works correctly with multiple conditions.

    The ``_make_condition_dfs`` method is expected to:
        - Combine conditions for conditions with the same columns.

    Input:
        - Conditions

    Output:
        - Conditions as ``[DataFrame]``
    """
    # Setup
    column_values1 = {'column2': 'M'}
    column_values2 = {'column2': 'N'}
    conditions = [
        Condition(column_values=column_values1, num_rows=2),
        Condition(column_values=column_values2, num_rows=3),
    ]
    expected_conditions = pd.DataFrame([column_values1] * 2 + [column_values2] * 3)

    # Run
    result_conditions_list = model._make_condition_dfs(conditions=conditions)

    # Assert
    assert len(result_conditions_list) == 1
    result_conditions = result_conditions_list[0]
    assert isinstance(result_conditions, pd.DataFrame)
    assert len(result_conditions) == 5
    assert all(result_conditions == expected_conditions)


@pytest.mark.parametrize('model', MODELS)
def test__make_condition_dfs_with_multiple_conditions_different_columns(model):
    """Test ``_make_condition_dfs`` works correctly with multiple conditions.

    The ``_make_condition_dfs`` method is expected to:
        - Return multiple DataFrames if conditions are not able to be combined.

    Input:
        - Conditions

    Output:
        - Conditions as ``[DataFrame]``
    """
    # Setup
    column_values1 = {'column2': 'M'}
    column_values2 = {'column3': 'N'}
    conditions = [
        Condition(column_values=column_values1, num_rows=2),
        Condition(column_values=column_values2, num_rows=3),
    ]
    expected_conditions1 = pd.DataFrame([column_values1] * 2)
    expected_conditions2 = pd.DataFrame([column_values2] * 3)

    # Run
    result_conditions_list = model._make_condition_dfs(conditions=conditions)

    # Assert
    assert len(result_conditions_list) == 2

    result_conditions1 = result_conditions_list[0]
    assert isinstance(result_conditions1, pd.DataFrame)
    assert len(result_conditions1) == 2
    assert all(result_conditions1 == expected_conditions1)

    result_conditions2 = result_conditions_list[1]
    assert isinstance(result_conditions2, pd.DataFrame)
    assert len(result_conditions2) == 3
    assert all(result_conditions2 == expected_conditions2)


def test__randomize_samples_true():
    """Test that ``_randomize_samples`` sets the random state correctly.

    Input:
        - randomize_samples as True

    Side Effect:
        - random state is set
    """
    # Setup
    instance = Mock()
    randomize_samples = True

    # Run
    BaseTabularModel._randomize_samples(instance, randomize_samples)

    # Assert
    assert instance._set_random_state.called_once_with(FIXED_RNG_SEED)


def test__randomize_samples_false():
    """Test that ``_randomize_samples`` is a no-op when user wants random samples.

    Input:
        - randomize_samples as False
    """
    # Setup
    instance = Mock()
    randomize_samples = False

    # Run
    BaseTabularModel._randomize_samples(instance, randomize_samples)

    # Assert
    assert instance._set_random_state.called_once_with(None)
