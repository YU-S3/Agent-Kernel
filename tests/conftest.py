import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item,call):
    outcome=yield
    setattr(item,'rep_'+call.when,outcome.get_result())
