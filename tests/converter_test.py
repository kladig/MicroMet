import pytest
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
import tempfile
import os

from micromet.converter import Reformatter, dataframe_from_file, raw_file_compile, header_dict


@pytest.fixture
def sample_ec_data():
    """Create sample eddy covariance data for testing"""
    dates = pd.date_range(start='2024-01-01', end='2024-01-02', freq='30min')
    data = {
        'TIMESTAMP_START': [int(d.strftime('%Y%m%d%H%M')) for d in dates],
        'TIMESTAMP_END': [int((d + pd.Timedelta(minutes=30)).strftime('%Y%m%d%H%M')) for d in dates],
        'CO2': np.random.normal(400, 10, len(dates)),
        'H2O': np.random.normal(10, 2, len(dates)),
        'TA_1_1_1': np.random.normal(20, 5, len(dates)),
        'Ts': np.random.normal(20, 5, len(dates)),
        'Ux': np.random.normal(0, 2, len(dates)),
        'Uy': np.random.normal(0, 2, len(dates)),
        'Uz': np.random.normal(0, 0.5, len(dates)),
        'Pr': np.random.normal(101.3, 0.5, len(dates)) * 1000,  # Pa
        'pV': np.random.normal(0.01, 0.002, len(dates)),
        'volt_KH20': np.random.normal(2, 0.2, len(dates))
    }
    return pd.DataFrame(data)


@pytest.fixture
def temp_data_file(sample_ec_data):
    """Create a temporary CSV file with sample data"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as tf:
        sample_ec_data.to_csv(tf.name, index=False)
        return tf.name


def test_dataframe_from_file(temp_data_file):
    """Test reading data from file"""
    df = dataframe_from_file(temp_data_file)

    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert 'TIMESTAMP_START' in df.columns
    assert 'CO2' in df.columns

    # Test handling of missing/invalid file
    result = dataframe_from_file('nonexistent_file.csv')
    assert result is None


def test_reformatter_initialization(sample_ec_data):
    """Test Reformatter class initialization"""
    reformatter = Reformatter(sample_ec_data)

    assert reformatter.et_data is not None
    assert isinstance(reformatter.et_data, pd.DataFrame)
    assert hasattr(reformatter, 'varlimits')


def test_datefixer(sample_ec_data):
    """Test date fixing functionality"""
    reformatter = Reformatter(sample_ec_data)
    fixed_df = reformatter.datefixer(sample_ec_data)

    assert isinstance(fixed_df.index, pd.DatetimeIndex)
    assert fixed_df.index.is_monotonic_increasing
    assert not fixed_df.index.has_duplicates

    # Test handling of invalid dates
    bad_dates_df = sample_ec_data.copy()
    bad_dates_df.loc[0, 'TIMESTAMP_START'] = 999999999999
    with pytest.raises(Exception):
        reformatter.datefixer(bad_dates_df)


def test_extreme_limiter(sample_ec_data):
    """Test removal of extreme values"""
    reformatter = Reformatter(sample_ec_data)
    limited_df = reformatter.extreme_limiter(sample_ec_data)

    # Check that values are within physical limits
    assert limited_df['TA_1_1_1'].max() <= reformatter.varlimits.loc['TA', 'Max']
    assert limited_df['TA_1_1_1'].min() >= reformatter.varlimits.loc['TA', 'Min']


def test_despike(sample_ec_data):
    """Test despiking functionality"""
    reformatter = Reformatter(sample_ec_data)

    # Add some artificial spikes
    test_data = sample_ec_data['Ux'].copy()
    test_data[10:15] = test_data.mean() + 10 * test_data.std()

    despiked_data = reformatter.despike(test_data)

    assert len(despiked_data) == len(test_data)
    assert despiked_data.std() < test_data.std()


def test_name_changer(sample_ec_data):
    """Test variable name standardization"""
    reformatter = Reformatter(sample_ec_data)

    # Add some non-standard names that should be changed
    test_df = sample_ec_data.copy()
    test_df['TA_2_1_1'] = test_df['TA_1_1_1']

    reformatter.et_data = test_df
    reformatter.name_changer()

    assert 'TA_1_2_1' in reformatter.et_data.columns
    assert 'TA_2_1_1' not in reformatter.et_data.columns


def test_ssitc_scale(sample_ec_data):
    """Test SSITC quality value scaling"""
    reformatter = Reformatter(sample_ec_data)

    # Add test SSITC columns
    test_df = sample_ec_data.copy()
    test_df['FC_SSITC_TEST'] = np.random.randint(1, 10, len(test_df))

    reformatter.et_data = test_df
    reformatter.ssitc_scale()

    # Check that values are scaled to 0-2 range
    assert reformatter.et_data['FC_SSITC_TEST'].max() <= 2
    assert reformatter.et_data['FC_SSITC_TEST'].min() >= 0


def test_tau_fixer(sample_ec_data):
    """Test TAU sign correction"""
    reformatter = Reformatter(sample_ec_data)

    # Add test TAU column
    test_df = sample_ec_data.copy()
    test_df['TAU'] = np.random.normal(0.1, 0.02, len(test_df))
    original_tau = test_df['TAU'].copy()

    reformatter.et_data = test_df
    reformatter.tau_fixer()

    # Check that signs are inverted
    assert np.allclose(reformatter.et_data['TAU'], -original_tau)


def test_fix_swc_percent(sample_ec_data):
    """Test soil water content percentage conversion"""
    reformatter = Reformatter(sample_ec_data)

    # Add test SWC column in decimal form
    test_df = sample_ec_data.copy()
    test_df['SWC_1_1_1'] = np.random.uniform(0.1, 0.5, len(test_df))
    original_swc = test_df['SWC_1_1_1'].copy()

    reformatter.et_data = test_df
    reformatter.fix_swc_percent()

    # Check that values are converted to percentages
    assert np.allclose(reformatter.et_data['SWC_1_1_1'], original_swc * 100)


def test_raw_file_compile():
    """Test compilation of raw files"""
    # Create temporary directory with test files
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create sample files
        for i in range(3):
            df = pd.DataFrame({
                'TIMESTAMP_START': pd.date_range(f'2024-01-0{i + 1}', periods=48, freq='30min').strftime(
                    '%Y%m%d%H%M').astype(int),
                'CO2': np.random.normal(400, 10, 48)
            })
            df.to_csv(f"{tmpdir}/Flux_AmeriFluxFormat_{i}.dat", index=False)

        # Test compilation
        result = raw_file_compile(Path(tmpdir), '')

        assert isinstance(result, pd.DataFrame)
        assert len(result) == 144  # 3 days * 48 records per day
        assert 'CO2' in result.columns


# Add new test for the main processing functions
def test_data_preprocessing(sample_ec_data):
    """Test data preprocessing steps"""
    reformatter = Reformatter(sample_ec_data)

    # Test despiking
    for field in reformatter.despikey:
        if field in sample_ec_data.columns:
            # Add artificial spikes
            sample_ec_data[field].iloc[10:15] = sample_ec_data[field].mean() + 10 * sample_ec_data[field].std()

            # Verify despiking removes outliers
            despiked = reformatter.despike(sample_ec_data[field])
            assert despiked.std() < sample_ec_data[field].std()


if __name__ == '__main__':
    pytest.main([__file__])