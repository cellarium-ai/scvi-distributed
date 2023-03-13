from contextlib import contextmanager
from typing import List, Optional, Sequence, Tuple, Union

import pandas as pd
from anndata import AnnData
from anndata._core.index import Index, _normalize_indices
from anndata.experimental.multi_files._anncollection import (
    AnnCollection,
    AnnCollectionView,
    ConvertType,
)
from boltons.cacheutils import LRU, cachedproperty
from braceexpand import braceexpand

from .read import read_h5ad_file
from .schema import AnnDataSchema


class getattr_mode:
    lazy = False


_GETATTR_MODE = getattr_mode()


@contextmanager
def lazy_getattr():
    try:
        _GETATTR_MODE.lazy = True
        yield
    finally:
        _GETATTR_MODE.lazy = False


class DistributedAnnDataCollection(AnnCollection):
    r"""
    Distributed AnnData Collection.

    This class is a wrapper around AnnCollection where adatas is a list
    of LazyAnnData objects.

    Underlying anndata files must conform to the same schema (see `AnnDataSchema.validate_anndata`)
    The schema is inferred from the first AnnData file in the collection. Individual AnnData files may
    otherwise vary in the number of cells, and the actual content stored in `.X`, `.layers`, `.obs` and `.obsm`.

    Example::

        >>> dadc = DistributedAnnDataCollection(
        ...     "gs://bucket-name/folder/adata{000..005}.h5ad",
        ...     shard_size=10000,
        ...     max_cache_size=2)

    Args:
        filenames: Names of anndata files.
        limits: Limits of cell indices.
        shard_size: Shard size.
        last_shard_size: Last shard size.
        max_cache_size: Max size of the cache.
        cache_size_strictly_enforced: Assert that the number of retrieved anndatas is not more than maxsize.
        label: Column in `.obs` to place batch information in. If it's None, no column is added.
        keys: Names for each object being added. These values are used for column values for
            `label` or appended to the index if `index_unique` is not `None`. Defaults to filenames.
        index_unique: Whether to make the index unique by using the keys. If provided, this
            is the delimeter between "{orig_idx}{index_unique}{key}". When `None`,
            the original indices are kept.
        convert: You can pass a function or a Mapping of functions which will be applied
            to the values of attributes (`.obs`, `.obsm`, `.layers`, `.X`) or to specific
            keys of these attributes in the subset object.
            Specify an attribute and a key (if needed) as keys of the passed Mapping
            and a function to be applied as a value.
        indices_strict: If  `True`, arrays from the subset objects will always have the same order
            of indices as in selection used to subset.
            This parameter can be set to `False` if the order in the returned arrays
            is not important, for example, when using them for stochastic gradient descent.
            In this case the performance of subsetting can be a bit better.
    """

    def __init__(
        self,
        filenames: Union[Sequence[str], str],
        limits: Optional[Sequence[int]] = None,
        shard_size: Optional[int] = None,
        last_shard_size: Optional[int] = None,
        max_cache_size: Optional[int] = None,
        cache_size_strictly_enforced: bool = True,
        label: Optional[str] = None,
        keys: Optional[Sequence[str]] = None,
        index_unique: Optional[str] = None,
        convert: Optional[ConvertType] = None,
        indices_strict: bool = True,
    ):
        if isinstance(filenames, str):
            filenames = braceexpand(filenames)
        self.filenames = list(filenames)
        assert isinstance(self.filenames[0], str)
        if (limits is None) is (shard_size is None):
            raise ValueError(
                "Either `limits` or `shard_size` must be specified, but not both."
            )
        elif (shard_size is None) and (last_shard_size is not None):
            raise ValueError(
                "If `last_shard_size` is specified then `shard_size` must also be specified."
            )
        if shard_size is not None:
            limits = [shard_size * (i + 1) for i in range(len(self.filenames))]
            if last_shard_size is not None:
                limits[-1] = limits[-1] - shard_size + last_shard_size
        else:
            limits = list(limits)
        assert len(limits) == len(self.filenames)
        # lru cache
        self.cache = LRU(max_cache_size)
        self.max_cache_size = max_cache_size
        self.cache_size_strictly_enforced = cache_size_strictly_enforced
        # schema
        adata0 = self.cache[self.filenames[0]] = read_h5ad_file(self.filenames[0])
        self.schema = AnnDataSchema(adata0)
        # lazy anndatas
        lazy_adatas = [
            LazyAnnData(filename, (start, end), self.schema, self.cache)
            for start, end, filename in zip([0] + limits, limits, self.filenames)
        ]
        # use filenames as default keys
        if keys is None:
            keys = self.filenames
        assert len(keys) == len(self.filenames)
        with lazy_getattr():
            super().__init__(
                adatas=lazy_adatas,
                join_obs=None,
                join_obsm=None,
                join_vars=None,
                label=label,
                keys=keys,
                index_unique=index_unique,
                convert=convert,
                harmonize_dtypes=False,
                indices_strict=indices_strict,
            )

    def __getitem__(self, index: Index) -> AnnCollectionView:
        oidx, vidx = _normalize_indices(index, self.obs_names, self.var_names)
        resolved_idx = self._resolve_idx(oidx, vidx)
        adatas_indices = [i for i, e in enumerate(resolved_idx[0]) if e is not None]
        # TODO: materialize at the last moment?
        self.materialize(adatas_indices)

        return AnnCollectionView(self, self.convert, resolved_idx)

    def materialize(self, indices: Union[int, Sequence[int]]) -> List[AnnData]:
        """
        Buffer and return anndata files at given indices from the list of lazy anndatas.

        This efficiently first retrieves cached files and only then caches new files.
        """
        if isinstance(indices, int):
            indices = (indices,)
        if self.cache_size_strictly_enforced:
            assert len(indices) <= self.max_cache_size, (
                f"Expected the number of anndata files ({len(indices)}) to be "
                f"no more than the max cache size ({self.max_cache_size})."
            )
        adatas = [None] * len(indices)
        # first fetch cached anndata files
        # this ensures that they are not popped if they were lru
        for i, idx in enumerate(indices):
            if self.adatas[idx].cached:
                adatas[i] = self.adatas[idx].adata
        # only then cache new anndata files
        for i, idx in enumerate(indices):
            if not self.adatas[idx].cached:
                adatas[i] = self.adatas[idx].adata
        return adatas

    def __repr__(self) -> str:
        n_obs, n_vars = self.shape
        descr = f"DistributedAnnDataCollection object with n_obs × n_vars = {self.n_obs} × {self.n_vars}"
        descr += f"\n  constructed from {len(self.filenames)} AnnData objects"
        for attr, keys in self._view_attrs_keys.items():
            if len(keys) > 0:
                descr += f"\n    view of {attr}: {str(keys)[1:-1]}"
        for attr in self._attrs:
            keys = list(getattr(self, attr).keys())
            if len(keys) > 0:
                descr += f"\n    {attr}: {str(keys)[1:-1]}"
        if "obs" in self._view_attrs_keys:
            keys = list(self.obs.keys())
            if len(keys) > 0:
                descr += f"\n    own obs: {str(keys)[1:-1]}"

        return descr

    def __getstate__(self):
        state = self.__dict__.copy()
        del state["cache"]
        del state["adatas"]
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.cache = LRU(self.max_cache_size)
        self.adatas = [
            LazyAnnData(filename, (start, end), self.schema, self.cache)
            for start, end, filename in zip(
                [0] + self.limits, self.limits, self.filenames
            )
        ]


class LazyAnnData:
    r"""
    Lazy AnnData backed by a file.

    Accessing attributes under `lazy_getattr` context returns schema attributes.

    Args:
        filename (str): Name of anndata file.
        limits (Tuple[int, int]): Limits of cell indices (inclusive, exclusive).
        schema (AnnDataSchema): Schema used as a reference for lazy attributes.
        cache (LRU): Shared LRU cache storing buffered anndatas.
    """

    _lazy_attrs = ["obs", "obsm", "layers", "var", "varm", "varp", "var_names"]
    _all_attrs = [
        "obs",
        "var",
        "uns",
        "obsm",
        "varm",
        "layers",
        "obsp",
        "varp",
    ]

    def __init__(
        self,
        filename: str,
        limits: Tuple[int, int],
        schema: AnnDataSchema,
        cache: Optional[LRU] = None,
    ):
        self.filename = filename
        self.limits = limits
        self.schema = schema
        if cache is None:
            cache = LRU()
        self.cache = cache

    @property
    def n_obs(self) -> int:
        return self.limits[1] - self.limits[0]

    @property
    def n_vars(self) -> int:
        return len(self.var_names)

    @property
    def shape(self) -> Tuple[int, int]:
        return self.n_obs, self.n_vars

    @cachedproperty
    def obs_names(self) -> pd.Index:
        """This is different from the backed anndata"""
        return pd.Index([f"cell_{i}" for i in range(*self.limits)])

    @property
    def cached(self) -> bool:
        return self.filename in self.cache

    @property
    def adata(self) -> AnnData:
        """Return backed anndata from the filename"""
        try:
            adata = self.cache[self.filename]
        except KeyError:
            # fetch anndata
            adata = read_h5ad_file(self.filename)
            # validate anndata
            assert self.n_obs == adata.n_obs, (
                "Expected n_obs for LazyAnnData object and backed anndata to match "
                f"but found {self.n_obs} and {adata.n_obs}, respectively."
            )
            self.schema.validate_anndata(adata)
            # cache anndata
            self.cache[self.filename] = adata
        return adata

    def __getattr__(self, attr):
        if _GETATTR_MODE.lazy:
            # This is only used during the initialization of DistributedAnnDataCollection
            if attr in self._lazy_attrs:
                return self.schema.attr_values[attr]
            raise AttributeError(f"Lazy AnnData object has no attribute '{attr}'")
        else:
            adata = self.adata
            if hasattr(adata, attr):
                return getattr(adata, attr)
            raise AttributeError(f"Backed AnnData object has no attribute '{attr}'")

    def __getitem__(self, idx) -> AnnData:
        return self.adata[idx]

    def __repr__(self) -> str:
        if self.cached:
            buffered = "Cached "
        else:
            buffered = ""
        backed_at = f" backed at {str(self.filename)!r}"
        descr = f"{buffered}LazyAnnData object with n_obs × n_vars = {self.n_obs} × {self.n_vars}{backed_at}"
        if self.cached:
            for attr in self._all_attrs:
                keys = getattr(self, attr).keys()
                if len(keys) > 0:
                    descr += f"\n    {attr}: {str(list(keys))[1:-1]}"
        return descr
