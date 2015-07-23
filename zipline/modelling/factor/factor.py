"""
factor.py
"""
from operator import attrgetter
from numpy import (
    apply_along_axis,
    float64,
    nan,
)
from scipy.stats import rankdata

from zipline.errors import (
    UnknownRankMethod,
    UnsupportedDataType,
)
from zipline.modelling.term import (
    CustomTermMixin,
    RequiredWindowLengthMixin,
    SingleInputMixin,
    Term,
    TestingTermMixin,
)
from zipline.modelling.expression import (
    bad_op,
    COMPARISONS,
    is_comparison,
    MATH_BINOPS,
    method_name_for_op,
    NUMERIC_TYPES,
    NumericalExpression,
    NUMEXPR_MATH_FUNCS,
    UNARY_OPS,
)
from zipline.modelling.filter import (
    NumExprFilter,
    PercentileFilter,
)
from zipline.utils.control_flow import nullctx


_RANK_METHODS = frozenset(['average', 'min', 'max', 'dense', 'ordinal'])


def binop_return_type(op):
    if is_comparison(op):
        return NumExprFilter
    else:
        return NumExprFactor


def binary_operator(op):
    """
    Factory function for making binary operator methods on a Factor subclass.

    Returns a function, "binary_operator" suitable for implementing functions
    like __add__.
    """
    # When combining a Factor with a NumericalExpression, we use this
    # attrgetter instance to defer to the commuted implementation of the
    # NumericalExpression operator.
    commuted_method_getter = attrgetter(method_name_for_op(op, commute=True))

    def binary_operator(self, other):
        # This can't be hoisted up a scope because the types returned by
        # binop_return_type aren't defined when the top-level function is
        # invoked in the class body of Factor.
        return_type = binop_return_type(op)
        if isinstance(self, NumExprFactor):
            self_expr, other_expr, new_inputs = self.build_binary_op(
                op, other,
            )
            return return_type(
                "({left}) {op} ({right})".format(
                    left=self_expr,
                    op=op,
                    right=other_expr,
                ),
                new_inputs,
            )
        elif isinstance(other, NumExprFactor):
            # NumericalExpression overrides ops to correctly handle merging of
            # inputs.  Look up and call the appropriate reflected operator with
            # ourself as the input.
            return commuted_method_getter(other)(self)
        elif isinstance(other, Factor):
            if self is other:
                return return_type(
                    "x_0 {op} x_0".format(op=op),
                    (self,),
                )
            return return_type(
                "x_0 {op} x_1".format(op=op),
                (self, other),
            )
        elif isinstance(other, NUMERIC_TYPES):
            return return_type(
                "x_0 {op} ({constant})".format(op=op, constant=other),
                binds=(self,),
            )
        raise bad_op(op, self, other)

    return binary_operator


def reflected_binary_operator(op):
    """
    Factory function for making binary operator methods on a Factor.

    Returns a function, "reflected_binary_operator" suitable for implementing
    functions like __radd__.
    """
    assert not is_comparison(op)

    def reflected_binary_operator(self, other):

        if isinstance(self, NumericalExpression):
            self_expr, other_expr, new_inputs = self.build_binary_op(
                op, other
            )
            return NumExprFactor(
                "({left}) {op} ({right})".format(
                    left=other_expr,
                    right=self_expr,
                    op=op,
                ),
                new_inputs,
            )

        # Only have to handle the numeric case because in all other valid cases
        # the corresponding left-binding method will be called.
        elif isinstance(other, NUMERIC_TYPES):
            return NumExprFactor(
                "{constant} {op} x_0".format(op=op, constant=other),
                binds=(self,),
            )
        raise bad_op(op, other, self)
    return reflected_binary_operator


def unary_operator(op):
    """
    Factory function for making unary operator methods for Factors.
    """
    # Only negate is currently supported for all our possible input types.
    valid_ops = {'-'}
    if op not in valid_ops:
        raise ValueError("Invalid unary operator %s." % op)

    def unary_operator(self):
        # This can't be hoisted up a scope because the types returned by
        # unary_op_return_type aren't defined when the top-level function is
        # invoked.
        if isinstance(self, NumericalExpression):
            return NumExprFactor(
                "{op}({expr})".format(op=op, expr=self._expr),
                self.inputs,
            )
        else:
            return NumExprFactor("{op}x_0".format(op=op), (self,))
    return unary_operator


def function_application(func):
    """
    Factory function for producing function application methods for Factor
    subclasses.
    """
    if func not in NUMEXPR_MATH_FUNCS:
        raise ValueError("Unsupported mathematical function '%s'" % func)

    def mathfunc(self):
        if isinstance(self, NumericalExpression):
            return NumExprFactor(
                "{func}({expr})".format(func=func, expr=self._expr),
                self.inputs,
            )
        else:
            return NumExprFactor("{func}(x_0)".format(func=func), (self,))
    return mathfunc


class Factor(Term):
    """
    A transformation yielding a timeseries of scalar values associated with an
    Asset.
    """
    # Dynamically add functions for creating NumExprFactor/NumExprFilter
    # instances.
    clsdict = locals()
    clsdict.update(
        {
            method_name_for_op(op): binary_operator(op)
            # Don't override __eq__ because it breaks comparisons on tuples of
            # Factors.
            for op in MATH_BINOPS.union(COMPARISONS - {'=='})
        }
    )
    clsdict.update(
        {
            method_name_for_op(op, commute=True): reflected_binary_operator(op)
            for op in MATH_BINOPS
        }
    )
    clsdict.update(
        {
            '__neg__': unary_operator(op)
            for op in UNARY_OPS
        }
    )
    clsdict.update(
        {
            funcname: function_application(funcname)
            for funcname in NUMEXPR_MATH_FUNCS
        }
    )

    __truediv__ = clsdict['__div__']
    __rtruediv__ = clsdict['__rdiv__']

    eq = binary_operator('==')

    def rank(self, method='ordinal'):
        """
        Construct a new Factor representing the sorted rank of each column
        within each row.

        Returns
        -------
        ranks : zipline.modelling.factor.Rank
            A new factor that will compute the sorted indices of the data
            produced by `self`.
        method : str, {'ordinal', 'min', 'max', 'dense', 'average'}
            The method used to assign ranks to tied elements. Default is
            'ordinal'.  See `scipy.stats.rankdata` for a full description of
            the semantics for each ranking method.

            The default is 'ordinal'.

        Notes
        -----
        The default value for `method` is different from the default for
        `scipy.stats.rankdata`.  See that function's documentation for a full
        description of the valid inputs to `method`.

        Missing or non-existent data on a given day will cause an asset to be
        given a rank of NaN for that day.

        See Also
        --------
        scipy.stats.rankdata : Underlying ranking algorithm.
        zipline.modelling.factor.Rank : Class implementing core functionality.
        """
        return Rank(self, method=method)

    def percentile_between(self, min_percentile, max_percentile):
        """
        Construct a new Filter representing entries from the output of this
        Factor that fall within the percentile range defined by min_percentile
        and max_percentile.

        Parameters
        ----------
        min_percentile : float [0.0, 100.0]
        max_percentile : float [0.0, 100.0]

        Returns
        -------
        out : zipline.modelling.filter.PercentileFilter
            A new filter that will compute the specified percentile-range mask.

        See Also
        --------
        zipline.modelling.filter.PercentileFilter
        """
        return PercentileFilter(
            self,
            min_percentile=min_percentile,
            max_percentile=max_percentile,
        )


class NumExprFactor(NumericalExpression, Factor):
    """
    Factor computed from a numexpr expression.

    Parameters
    ----------
    expr : string
       A string suitable for passing to numexpr.  All variables in 'expr'
       should be of the form "x_i", where i is the index of the corresponding
       factor input in 'binds'.
    binds : tuple
       A tuple of factors to use as inputs.

    Notes
    -----
    NumExprFactors are constructed by numerical operators like `+` and `-`.
    Users should rarely need to construct a NumExprFactor directly.
    """
    pass


class Rank(SingleInputMixin, Factor):
    """
    A Factor representing the row-wise rank data of another Factor.

    Parameters
    ----------
    factor : zipline.modelling.factor.Factor
        The factor on which to compute ranks.
    method : str, {'average', 'min', 'max', 'dense', 'ordinal'}
        The method used to assign ranks to tied elements.  See
        `scipy.stats.rankdata` for a full description of the semantics for each
        ranking method.

    See Also
    --------
    scipy.stats.rankdata : Underlying ranking algorithm.
    zipline.factor.Factor.rank : Method-style interface to same functionality.

    Notes
    -----
    Most users should call Factor.rank rather than directly construct an
    instance of this class.
    """
    dtype = float64
    window_length = 0
    domain = None

    def __new__(cls, factor, method):
        return super(Rank, cls).__new__(
            cls,
            inputs=(factor,),
            method=method,
        )

    def _init(self, method, *args, **kwargs):
        self._method = method
        return super(Rank, self)._init(*args, **kwargs)

    @classmethod
    def static_identity(cls, method, *args, **kwargs):
        return (
            super(Rank, cls).static_identity(*args, **kwargs),
            method,
        )

    def _validate(self):
        """
        Verify that the stored rank method is valid.
        """
        if self._method not in _RANK_METHODS:
            raise UnknownRankMethod(
                method=self._method,
                choices=set(_RANK_METHODS),
            )
        return super(Rank, self)._validate()

    def compute_from_arrays(self, arrays, mask):
        """
        For each row in the input, compute a like-shaped array of per-row
        ranks.
        """
        # FUTURE OPTIMIZATION:
        # Write a less general `apply_to_rows` method in
        # Cython that doesn't do all the extra work that apply_over_axis does.

        # FUTURE OPTIMIZATION:
        # Look at bottleneck.nanrankdata, which is ~30% faster than numpy here,
        # and does what we want with NaNs, but doesn't support `method`.
        result = apply_along_axis(
            rankdata,
            1,
            arrays[0],
            method=self._method,
        )
        # rankdata will sort nan values into last place, but we want our nans
        # to propagate, so explicitly re-apply
        result[~mask.values] = nan
        return result

    def __repr__(self):
        return "{type}({input_}, method='{method}')".format(
            type=type(self).__name__,
            input_=self.inputs[0],
            method=self._method,
        )


class CustomFactor(RequiredWindowLengthMixin, CustomTermMixin, Factor):
    """
    Base class for user-defined Factors operating on windows of raw data.

    TODO: This is basically the most important class to document in the whole
    FFC API...

    We currently only support CustomFactors of type float64.
    """
    dtype = float64
    ctx = nullctx()

    def _validate(self):
        if self.dtype != float64:
            raise UnsupportedDataType(self.dtype)
        return super(CustomFactor, self)._validate()


class TestingFactor(TestingTermMixin, Factor):
    """
    Base class for testing engines that asserts all inputs are correctly
    shaped.
    """
    pass
