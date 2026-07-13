import duckhaven_sql_connector


def test_package_imports_and_exposes_version():
    assert isinstance(duckhaven_sql_connector.__version__, str)
    assert duckhaven_sql_connector.__version__
