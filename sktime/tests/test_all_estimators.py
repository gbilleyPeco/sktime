# -*- coding: utf-8 -*-
# copyright: sktime developers, BSD-3-Clause License (see LICENSE file)
"""Suite of tests for all estimators.

adapted from scikit-learn's estimator_checks
"""

__author__ = ["mloning", "fkiraly", "achieveordie"]

import numbers
import os
import types
from copy import deepcopy
from inspect import getfullargspec, isclass, signature
from tempfile import TemporaryDirectory
from warnings import warn

import joblib
import numpy as np
import pytest
from skbase.testing import BaseFixtureGenerator as _BaseFixtureGenerator
from skbase.testing import QuickTester as _QuickTester
from skbase.testing import TestAllObjects as _TestAllObjects
from sklearn.utils._testing import set_random_state
from sklearn.utils.estimator_checks import (
    check_get_params_invariance as _check_get_params_invariance,
)

from sktime.base import BaseEstimator, BaseObject, load
from sktime.classification.deep_learning.base import BaseDeepClassifier
from sktime.dists_kernels._base import (
    BasePairwiseTransformer,
    BasePairwiseTransformerPanel,
)
from sktime.exceptions import NotFittedError
from sktime.forecasting.base import BaseForecaster
from sktime.registry import all_estimators
from sktime.regression.deep_learning.base import BaseDeepRegressor
from sktime.tests._config import (
    EXCLUDE_ESTIMATORS,
    EXCLUDED_TESTS,
    NON_STATE_CHANGING_METHODS,
    NON_STATE_CHANGING_METHODS_ARRAYLIKE,
    VALID_ESTIMATOR_BASE_TYPES,
    VALID_ESTIMATOR_TAGS,
    VALID_ESTIMATOR_TYPES,
    VALID_TRANSFORMER_TYPES,
)
from sktime.utils._testing._conditional_fixtures import (
    create_conditional_fixtures_and_names,
)
from sktime.utils._testing.deep_equals import deep_equals
from sktime.utils._testing.estimator_checks import (
    _assert_array_almost_equal,
    _assert_array_equal,
    _get_args,
    _has_capability,
    _list_required_methods,
)
from sktime.utils._testing.scenarios_getter import retrieve_scenarios
from sktime.utils.sampling import random_partition
from sktime.utils.validation._dependencies import (
    _check_dl_dependencies,
    _check_estimator_deps,
    _check_soft_dependencies,
)

# whether to subsample estimators per os/version partition matrix design
# default is False, can be set to True by pytest --matrixdesign True flag
MATRIXDESIGN = False


def subsample_by_version_os(x):
    """Subsample objects by operating system and python version.

    Ensures each estimator is tested at least once on every OS and python version,
    if combined with a matrix of OS/versions.

    Currently assumes that matrix includes py3.8-3.10, and win/ubuntu/mac.
    """
    import platform
    import sys

    ix = sys.version_info.minor % 3
    os_str = platform.system()
    if os_str == "Windows":
        ix = ix
    elif os_str == "Linux":
        ix = ix + 1
    elif os_str == "Darwin":
        ix = ix + 2
    else:
        raise ValueError(f"found unexpected OS string: {os_str}")
    ix = ix % 3

    part = random_partition(len(x), 3)
    subset_idx = part[ix]
    res = [x[i] for i in subset_idx]

    return res


class BaseFixtureGenerator(_BaseFixtureGenerator):
    """Fixture generator for base testing functionality in sktime.

    Test classes inheriting from this and not overriding pytest_generate_tests
        will have estimator and scenario fixtures parametrized out of the box.

    Descendants can override:
        estimator_type_filter: str, class variable; None or scitype string
            e.g., "forecaster", "transformer", "classifier", see BASE_CLASS_SCITYPE_LIST
            which estimators are being retrieved and tested
        fixture_sequence: list of str
            sequence of fixture variable names in conditional fixture generation
        _generate_[variable]: object methods, all (test_name: str, **kwargs) -> list
            generating list of fixtures for fixture variable with name [variable]
                to be used in test with name test_name
            can optionally use values for fixtures earlier in fixture_sequence,
                these must be input as kwargs in a call
        is_excluded: static method (test_name: str, est: class) -> bool
            whether test with name test_name should be excluded for estimator est
                should be used only for encoding general rules, not individual skips
                individual skips should go on the EXCLUDED_TESTS list in _config
            requires _generate_estimator_class and _generate_estimator_instance as is
        _excluded_scenario: static method (test_name: str, scenario) -> bool
            whether scenario should be skipped in test with test_name test_name
            requires _generate_estimator_scenario as is

    Fixtures parametrized
    ---------------------
    estimator_class: estimator inheriting from BaseObject
        ranges over estimator classes not excluded by EXCLUDE_ESTIMATORS, EXCLUDED_TESTS
    estimator_instance: instance of estimator inheriting from BaseObject
        ranges over estimator classes not excluded by EXCLUDE_ESTIMATORS, EXCLUDED_TESTS
        instances are generated by create_test_instance class method of estimator_class
    scenario: instance of TestScenario
        ranges over all scenarios returned by retrieve_scenarios
        applicable for estimator_class or estimator_instance
    method_nsc: string, name of estimator method
        ranges over all "predict"-like, non-state-changing methods
        of estimator_instance or estimator_class that the class/object implements
    method_nsc_arraylike: string, for non-state-changing estimator methods
        ranges over all "predict"-like, non-state-changing estimator methods,
        which return an array-like output
    """

    # class variables to configure skbase BaseFixtureGenerator
    # --------------------------------------------------------

    # package to search for objects
    package_name = "sktime"

    # which object types are generated; None=all, or scitype string like "forecaster"
    object_type_filter = None

    # list of object types (class names) to exclude
    exclude_objects = None

    # list of tests to exclude
    excluded_tests = None

    # list of valid tags
    valid_tags = VALID_ESTIMATOR_TAGS

    # list of valid base type names
    valid_base_types = None

    # which sequence the conditional fixtures are generated in
    fixture_sequence = [
        "estimator_class",
        "estimator_instance",
        "scenario",
        "method_nsc",
        "method_nsc_arraylike",
    ]

    # which fixtures are indirect, e.g., have an additional pytest.fixture block
    #   to generate an indirect fixture at runtime. Example: estimator_instance
    #   warning: direct fixtures retain state changes within the same test
    indirect_fixtures = ["estimator_instance"]

    def _all_estimators(self):
        """Retrieve list of all estimator classes of type self.estimator_type_filter."""
        est_list = all_estimators(
            estimator_types=getattr(self, "estimator_type_filter", None),
            return_names=False,
            exclude_estimators=EXCLUDE_ESTIMATORS,
        )
        # subsample estimators by OS & python version
        # this ensures that only a 1/3 of estimators are tested for a given combination
        # but all are tested on every OS at least once, and on every python version once
        if MATRIXDESIGN:
            est_list = subsample_by_version_os(est_list)
        return est_list

    @staticmethod
    def is_excluded(test_name, est):
        """Shorthand to check whether test test_name is excluded for estimator est."""
        return test_name in EXCLUDED_TESTS.get(est.__name__, [])

    # the following functions define fixture generation logic for pytest_generate_tests
    # each function is of signature (test_name:str, **kwargs) -> List of fixtures
    # function with name _generate_[fixture_var] returns list of values for fixture_var
    #   where fixture_var is a fixture variable used in tests
    # the list is conditional on values of other fixtures which can be passed in kwargs

    def _generate_estimator_class(self, test_name, **kwargs):
        """Return estimator class fixtures.

        Fixtures parametrized
        ---------------------
        estimator_class: estimator inheriting from BaseObject
            ranges over all estimator classes not excluded by EXCLUDED_TESTS
        """
        estimator_classes_to_test = [
            est
            for est in self._all_estimators()
            if not self.is_excluded(test_name, est)
        ]

        # exclude classes based on python version compatibility
        estimator_classes_to_test = [
            est
            for est in estimator_classes_to_test
            if _check_estimator_deps(est, severity="none")
        ]

        estimator_names = [est.__name__ for est in estimator_classes_to_test]

        return estimator_classes_to_test, estimator_names

    def _generate_estimator_instance(self, test_name, **kwargs):
        """Return estimator instance fixtures.

        Fixtures parametrized
        ---------------------
        estimator_instance: instance of estimator inheriting from BaseObject
            ranges over all estimator classes not excluded by EXCLUDED_TESTS
            instances are generated by create_test_instance class method
        """
        # call _generate_estimator_class to get all the classes
        estimator_classes_to_test, _ = self._generate_estimator_class(
            test_name=test_name
        )

        # create instances from the classes
        estimator_instances_to_test = []
        estimator_instance_names = []
        # retrieve all estimator parameters if multiple, construct instances
        for est in estimator_classes_to_test:
            all_instances_of_est, instance_names = est.create_test_instances_and_names()
            estimator_instances_to_test += all_instances_of_est
            estimator_instance_names += instance_names

        return estimator_instances_to_test, estimator_instance_names

    # this is executed before each test instance call
    #   if this were not executed, estimator_instance would keep state changes
    #   within executions of the same test with different parameters
    @pytest.fixture(scope="function")
    def estimator_instance(self, request):
        """estimator_instance fixture definition for indirect use."""
        # esetimator_instance is cloned at the start of every test
        return request.param.clone()

    def _generate_scenario(self, test_name, **kwargs):
        """Return estimator test scenario.

        Fixtures parametrized
        ---------------------
        scenario: instance of TestScenario
            ranges over all scenarios returned by retrieve_scenarios
        """
        if "estimator_class" in kwargs.keys():
            obj = kwargs["estimator_class"]
        elif "estimator_instance" in kwargs.keys():
            obj = kwargs["estimator_instance"]
        else:
            return []

        scenarios = retrieve_scenarios(obj)
        scenarios = [s for s in scenarios if not self._excluded_scenario(test_name, s)]
        scenario_names = [type(scen).__name__ for scen in scenarios]

        return scenarios, scenario_names

    @staticmethod
    def _excluded_scenario(test_name, scenario):
        """Skip list generator for scenarios to skip in test_name.

        Arguments
        ---------
        test_name : str, name of test
        scenario : instance of TestScenario, to be used in test

        Returns
        -------
        bool, whether scenario should be skipped in test_name
        """
        # for forecasters tested in test_methods_do_not_change_state
        #   if fh is not passed in fit, then this test would fail
        #   since fh will be stored in predict through fh handling
        #   as there are scenarios which pass it early and everything else is the same
        #   we skip those scenarios
        if test_name == "test_methods_do_not_change_state":
            if not scenario.get_tag("fh_passed_in_fit", True, raise_error=False):
                return True

        # this line excludes all scenarios that do not have "is_enabled" flag
        #   we should slowly enable more scenarios for better coverage
        # comment out to run the full test suite with new scenarios
        if not scenario.get_tag("is_enabled", False, raise_error=False):
            return True

        return False

    def _generate_method_nsc(self, test_name, **kwargs):
        """Return estimator test scenario.

        Fixtures parametrized
        ---------------------
        method_nsc: string, for non-state-changing estimator methods
            ranges over all "predict"-like, non-state-changing estimator methods
        """
        # ensure cls is a class
        if "estimator_class" in kwargs.keys():
            obj = kwargs["estimator_class"]
            cls = obj
        elif "estimator_instance" in kwargs.keys():
            obj = kwargs["estimator_instance"]
            cls = type(obj)
        else:
            return []

        # complete list of all non-state-changing methods
        nsc_list = NON_STATE_CHANGING_METHODS

        # subset to the methods that x has implemented
        nsc_list = [x for x in nsc_list if _has_capability(obj, x)]

        # remove predict_proba for forecasters, if tensorflow-proba is not installed
        # this ensures that predict_proba, which requires it, is not called in testing
        if issubclass(cls, BaseForecaster):
            if not _check_dl_dependencies(severity="none"):
                nsc_list = list(set(nsc_list).difference(["predict_proba"]))

        return nsc_list

    def _generate_method_nsc_arraylike(self, test_name, **kwargs):
        """Return estimator test scenario.

        Fixtures parametrized
        ---------------------
        method_nsc_arraylike: string, for non-state-changing estimator methods
            ranges over all "predict"-like, non-state-changing estimator methods,
            which return an array-like output
        """
        method_nsc_list = self._generate_method_nsc(test_name=test_name, **kwargs)

        # subset to the arraylike ones to avoid copy-paste
        nsc_list_arraylike = set(method_nsc_list).intersection(
            NON_STATE_CHANGING_METHODS_ARRAYLIKE
        )
        return list(nsc_list_arraylike)


class QuickTester(_QuickTester):
    """Mixin class which adds the run_tests method to run tests on one estimator."""

    pass


class TestAllObjects(_TestAllObjects):
    """Package level tests for all sktime objects."""

    estimator_type_filter = "object"

    def test_inheritance(self, estimator_class):
        """Check that estimator inherits from BaseObject and/or BaseEstimator."""
        assert issubclass(
            estimator_class, BaseObject
        ), f"object {estimator_class} is not a sub-class of BaseObject."

        if hasattr(estimator_class, "fit"):
            assert issubclass(estimator_class, BaseEstimator), (
                f"estimator: {estimator_class} has fit method, but"
                f"is not a sub-class of BaseEstimator."
            )

        # Usually estimators inherit only from one BaseEstimator type, but in some cases
        # they may be predictor and transformer at the same time (e.g. pipelines)
        n_base_types = sum(
            issubclass(estimator_class, cls) for cls in VALID_ESTIMATOR_BASE_TYPES
        )

        assert 2 >= n_base_types >= 1

        # If the estimator inherits from more than one base estimator type, we check if
        # one of them is a transformer base type
        if n_base_types > 1:
            assert issubclass(estimator_class, VALID_TRANSFORMER_TYPES)

    def test_has_common_interface(self, estimator_class):
        """Check estimator implements the common interface."""
        estimator = estimator_class

        # Check class for type of attribute
        if isinstance(estimator_class, BaseEstimator):
            assert isinstance(estimator.is_fitted, property)

        required_methods = _list_required_methods(estimator_class)

        for attr in required_methods:
            assert hasattr(
                estimator, attr
            ), f"Estimator: {estimator.__name__} does not implement attribute: {attr}"

        if hasattr(estimator, "inverse_transform"):
            assert hasattr(estimator, "transform")
        if hasattr(estimator, "predict_proba"):
            assert hasattr(estimator, "predict")

    def test_clone(self, estimator_instance):
        """Check that clone method does not raise exceptions and results in a clone.

        A clone of an object x is an object that:
        * has same class and parameters as x
        * is not identical with x
        * is unfitted (even if x was fitted)
        """
        est_clone = estimator_instance.clone()
        assert isinstance(est_clone, type(estimator_instance))
        assert est_clone is not estimator_instance
        if hasattr(est_clone, "is_fitted"):
            assert not est_clone.is_fitted


class TestAllEstimators(BaseFixtureGenerator, QuickTester):
    """Package level tests for all sktime estimators, i.e., objects with fit."""

    def test_fit_updates_state(self, estimator_instance, scenario):
        """Check fit/update state change."""
        # Check that fit updates the is-fitted states
        attrs = ["_is_fitted", "is_fitted"]

        estimator = estimator_instance
        estimator_class = type(estimator_instance)

        msg = (
            f"{estimator_class.__name__}.__init__ should call "
            f"super({estimator_class.__name__}, self).__init__, "
            "but that does not seem to be the case. Please ensure to call the "
            f"parent class's constructor in {estimator_class.__name__}.__init__"
        )
        assert hasattr(estimator, "_is_fitted"), msg

        # Check is_fitted attribute is set correctly to False before fit, at init
        for attr in attrs:
            assert not getattr(
                estimator, attr
            ), f"Estimator: {estimator} does not initiate attribute: {attr} to False"

        fitted_estimator = scenario.run(estimator_instance, method_sequence=["fit"])

        # Check 0s_fitted attribute is updated correctly to False after calling fit
        for attr in attrs:
            assert getattr(
                fitted_estimator, attr
            ), f"Estimator: {estimator} does not update attribute: {attr} during fit"

    def test_fit_returns_self(self, estimator_instance, scenario):
        """Check that fit returns self."""
        fit_return = scenario.run(estimator_instance, method_sequence=["fit"])
        assert (
            fit_return is estimator_instance
        ), f"Estimator: {estimator_instance} does not return self when calling fit"

    def test_raises_not_fitted_error(self, estimator_instance, scenario, method_nsc):
        """Check exception raised for non-fit method calls to unfitted estimators.

        Tries to run all methods in NON_STATE_CHANGING_METHODS with valid scenario,
        but before fit has been called on the estimator.

        This should raise a NotFittedError if correctly caught,
        normally by a self.check_is_fitted() call in the method's boilerplate.

        Raises
        ------
        Exception if NotFittedError is not raised by non-state changing method
        """
        # pairwise transformers are exempted from this test, since they have no fitting
        PWTRAFOS = (BasePairwiseTransformer, BasePairwiseTransformerPanel)
        excepted = isinstance(estimator_instance, PWTRAFOS)
        if excepted:
            return None

        # call methods without prior fitting and check that they raise NotFittedError
        with pytest.raises(NotFittedError, match=r"has not been fitted"):
            scenario.run(estimator_instance, method_sequence=[method_nsc])

    def test_fit_idempotent(self, estimator_instance, scenario, method_nsc_arraylike):
        """Check that calling fit twice is equivalent to calling it once."""
        estimator = estimator_instance

        # for now, we have to skip predict_proba, since current output comparison
        #   does not work for tensorflow Distribution
        if (
            isinstance(estimator_instance, BaseForecaster)
            and method_nsc_arraylike == "predict_proba"
        ):
            return None

        # run fit plus method_nsc once, save results
        set_random_state(estimator)
        results = scenario.run(
            estimator,
            method_sequence=["fit", method_nsc_arraylike],
            return_all=True,
            deepcopy_return=True,
        )

        estimator = results[0]
        set_random_state(estimator)

        # run fit plus method_nsc a second time
        results_2nd = scenario.run(
            estimator,
            method_sequence=["fit", method_nsc_arraylike],
            return_all=True,
            deepcopy_return=True,
        )

        # check results are equal
        _assert_array_almost_equal(
            results[1],
            results_2nd[1],
            # err_msg=f"Idempotency check failed for method {method}",
        )

    def test_fit_does_not_overwrite_hyper_params(self, estimator_instance, scenario):
        """Check that we do not overwrite hyper-parameters in fit."""
        estimator = estimator_instance
        set_random_state(estimator)

        # Make a physical copy of the original estimator parameters before fitting.
        params = estimator.get_params()
        original_params = deepcopy(params)

        # Fit the model
        fitted_est = scenario.run(estimator_instance, method_sequence=["fit"])

        # Compare the state of the model parameters with the original parameters
        new_params = fitted_est.get_params()
        for param_name, original_value in original_params.items():
            new_value = new_params[param_name]

            # We should never change or mutate the internal state of input
            # parameters by default. To check this we use the joblib.hash function
            # that introspects recursively any subobjects to compute a checksum.
            # The only exception to this rule of immutable constructor parameters
            # is possible RandomState instance but in this check we explicitly
            # fixed the random_state params recursively to be integer seeds.
            assert joblib.hash(new_value) == joblib.hash(original_value), (
                "Estimator %s should not change or mutate "
                " the parameter %s from %s to %s during fit."
                % (estimator.__class__.__name__, param_name, original_value, new_value)
            )

    def test_non_state_changing_method_contract(
        self, estimator_instance, scenario, method_nsc
    ):
        """Check that non-state-changing methods behave as per interface contract.

        Check the following contract on non-state-changing methods:
        1. do not change state of the estimator, i.e., any attributes
            (including hyper-parameters and fitted parameters)
        2. expected output type of the method matches actual output type
            - only for abstract BaseEstimator methods, common to all estimator scitypes
            list of BaseEstimator methdos tested: get_fitted_params
            scitype specific method outputs are tested in TestAll[estimatortype] class
        """
        estimator = estimator_instance
        set_random_state(estimator)

        # dict_before = copy of dictionary of estimator before predict, post fit
        _ = scenario.run(estimator, method_sequence=["fit"])
        dict_before = estimator.__dict__.copy()

        # skip test if vectorization would be necessary and method predict_proba
        # this is since vectorization is not implemented for predict_proba
        if method_nsc == "predict_proba":
            try:
                scenario.run(estimator, method_sequence=[method_nsc])
            except NotImplementedError:
                return None

        # dict_after = dictionary of estimator after predict and fit
        output = scenario.run(estimator, method_sequence=[method_nsc])
        dict_after = estimator.__dict__

        is_equal, msg = deep_equals(dict_after, dict_before, return_msg=True)
        assert is_equal, (
            f"Estimator: {type(estimator).__name__} changes __dict__ "
            f"during {method_nsc}, "
            f"reason/location of discrepancy (x=after, y=before): {msg}"
        )

        # once there are more methods, this may have to be factored out
        # for now, there is only get_fitted_params and we test here to avoid fit calls
        if method_nsc == "get_fitted_params":
            msg = (
                f"get_fitted_params of {type(estimator)} should return dict, "
                f"but returns object of type {type(output)}"
            )
            assert isinstance(output, dict), msg
            msg = (
                f"get_fitted_params of {type(estimator)} should return dict with "
                f"with str keys, but some keys are not str"
            )
            nonstr = [x for x in output.keys() if not isinstance(x, str)]
            if not len(nonstr) == 0:
                msg = f"found non-str keys in get_fitted_params return: {nonstr}"
                raise AssertionError(msg)

    def test_methods_have_no_side_effects(
        self, estimator_instance, scenario, method_nsc
    ):
        """Check that calling methods has no side effects on args."""
        estimator = estimator_instance

        # skip test for get_fitted_params, as this does not have mutable arguments
        if method_nsc == "get_fitted_params":
            return None

        set_random_state(estimator)

        # Fit the model, get args before and after
        _, args_after = scenario.run(
            estimator, method_sequence=["fit"], return_args=True
        )
        fit_args_after = args_after[0]
        fit_args_before = scenario.args["fit"]

        assert deep_equals(
            fit_args_before, fit_args_after
        ), f"Estimator: {estimator} has side effects on arguments of fit"

        # skip test if vectorization would be necessary and method predict_proba
        # this is since vectorization is not implemented for predict_proba
        if method_nsc == "predict_proba":
            try:
                scenario.run(estimator, method_sequence=[method_nsc])
            except NotImplementedError:
                return None

        # Fit the model, get args before and after
        _, args_after = scenario.run(
            estimator, method_sequence=[method_nsc], return_args=True
        )
        method_args_after = args_after[0]
        method_args_before = scenario.get_args(method_nsc, estimator)

        assert deep_equals(
            method_args_after, method_args_before
        ), f"Estimator: {estimator} has side effects on arguments of {method_nsc}"

    def test_persistence_via_pickle(
        self, estimator_instance, scenario, method_nsc_arraylike
    ):
        """Check that we can pickle all estimators."""
        method_nsc = method_nsc_arraylike
        # escape predict_proba for forecasters, tfp distributions cannot be pickled
        if (
            isinstance(estimator_instance, BaseForecaster)
            and method_nsc == "predict_proba"
        ):
            return None
        # escape Deep estimators if soft-dep `h5py` isn't installed
        if isinstance(
            estimator_instance, (BaseDeepClassifier, BaseDeepRegressor)
        ) and not _check_soft_dependencies("h5py", severity="warning"):
            return None

        estimator = estimator_instance
        set_random_state(estimator)
        # Fit the model, get args before and after
        scenario.run(estimator, method_sequence=["fit"], return_args=True)

        # Generate results before pickling
        vanilla_result = scenario.run(estimator, method_sequence=[method_nsc])

        # Serialize and deserialize
        serialized_estimator = estimator.save()
        deserialized_estimator = load(serialized_estimator)

        deserialized_result = scenario.run(
            deserialized_estimator, method_sequence=[method_nsc]
        )

        msg = (
            f"Results of {method_nsc} differ between when pickling and not pickling, "
            f"estimator {type(estimator_instance).__name__}"
        )
        _assert_array_almost_equal(
            vanilla_result,
            deserialized_result,
            decimal=6,
            err_msg=msg,
        )

    def test_save_estimators_to_file(
        self, estimator_instance, scenario, method_nsc_arraylike
    ):
        """Check if saved estimators onto disk can be loaded correctly."""
        method_nsc = method_nsc_arraylike
        # escape predict_proba for forecasters, tfp distributions cannot be pickled
        if (
            isinstance(estimator_instance, BaseForecaster)
            and method_nsc == "predict_proba"
        ):
            return None

        estimator = estimator_instance
        set_random_state(estimator)
        # Fit the model, get args before and after
        scenario.run(estimator, method_sequence=["fit"], return_args=True)

        # Generate results before saving
        vanilla_result = scenario.run(estimator, method_sequence=[method_nsc])

        with TemporaryDirectory() as tmp_dir:
            save_loc = os.path.join(tmp_dir, "estimator")
            estimator.save(save_loc)

            loaded_estimator = load(save_loc)
            loaded_result = scenario.run(loaded_estimator, method_sequence=[method_nsc])

            msg = (
                f"Results of {method_nsc} differ between saved and loaded "
                f"estimator {type(estimator).__name__}"
            )

            _assert_array_almost_equal(
                vanilla_result,
                loaded_result,
                decimal=6,
                err_msg=msg,
            )

    # todo: this needs to be diagnosed and fixed - temporary skip
    @pytest.mark.skip(reason="hangs on mac and unix remote tests")
    def test_multiprocessing_idempotent(
        self, estimator_instance, scenario, method_nsc_arraylike
    ):
        """Test that single and multi-process run results are identical.

        Check that running an estimator on a single process is no different to running
        it on multiple processes. We also check that we can set n_jobs=-1 to make use
        of all CPUs. The test is not really necessary though, as we rely on joblib for
        parallelization and can trust that it works as expected.
        """
        method_nsc = method_nsc_arraylike
        params = estimator_instance.get_params()

        if "n_jobs" in params:
            # run on a single process
            # -----------------------
            estimator = deepcopy(estimator_instance)
            estimator.set_params(n_jobs=1)
            set_random_state(estimator)
            result_single_process = scenario.run(
                estimator, method_sequence=["fit", method_nsc]
            )

            # run on multiple processes
            # -------------------------
            estimator = deepcopy(estimator_instance)
            estimator.set_params(n_jobs=-1)
            set_random_state(estimator)
            result_multiple_process = scenario.run(
                estimator, method_sequence=["fit", method_nsc]
            )
            _assert_array_equal(
                result_single_process,
                result_multiple_process,
                err_msg="Results are not equal for n_jobs=1 and n_jobs=-1",
            )

    def test_dl_constructor_initializes_deeply(self, estimator_class):
        """Test DL estimators that they pass custom parameters to underlying Network."""
        estimator = estimator_class

        if not issubclass(estimator, (BaseDeepClassifier, BaseDeepRegressor)):
            return None

        if not hasattr(estimator, "get_test_params"):
            return None

        params = estimator.get_test_params()

        if isinstance(params, list):
            params = params[0]
        if isinstance(params, dict):
            pass
        else:
            raise TypeError(
                f"`get_test_params()` of estimator: {estimator} returns "
                f"an expected type: {type(params)}, acceptable formats: [list, dict]"
            )

        estimator = estimator(**params)

        for key, value in params.items():
            assert vars(estimator)[key] == value
            # some keys are only relevant to the final model (eg: n_epochs)
            # skip them for the underlying network
            if vars(estimator._network).get(key) is not None:
                assert vars(estimator._network)[key] == value

    def _get_err_msg(estimator):
        return (
            f"Invalid estimator type: {type(estimator)}. Valid estimator types are: "
            f"{VALID_ESTIMATOR_TYPES}"
        )
