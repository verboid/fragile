import copy

import numpy as np


def unique_columns2(data):

    dt = np.dtype((np.void, data.dtype.itemsize * data.shape[0]))
    dataf = np.asfortranarray(data).view(dt)
    u, uind = np.unique(dataf, return_inverse=True)
    u = u.view(data.dtype).reshape(-1, data.shape[0]).T
    return (u, uind)


class Encoder:
    def __init__(self, *args, **kwargs):
        pass

    def __len__(self):
        return 0

    def calculate_pest(self, mapper: "FunctionMapper"):
        pass

    def update(self, walkers_states=None, env_states=None, model_states=None):
        pass

    def reset(self, *args, **kwargs):
        pass


class Vector:
    def __init__(
        self,
        origin: np.ndarray = None,
        end: np.ndarray = None,
        timeout: int = 1e100,
        timeout_threshold: int = 100000,
    ):
        self.origin = origin
        self.end = end
        self.base = end - origin
        self._age = 0
        self.timeout = timeout
        self.last_regions = []
        self._pos_track = True
        self._neg_track = True
        self.timeout_threshold = timeout_threshold

    def __repr__(self):
        text = "Origin: {}, End: {} Age: {} Outdated: {}\n".format(
            self.origin, self.end, self._age, self.is_outdated()
        )
        return text

    def __hash__(self):
        return hash(str(np.concatenate([self.origin, self.end])))

    def scalar_product(self, other: np.ndarray):
        return np.dot(self.base, self.end - other)

    def assign_region(self, other: np.ndarray) -> int:

        region = 1 if self.scalar_product(other=other) > 0 else 0
        return region
        # if len(self.last_regions) < self.timeout:
        #    self.last_regions.append(region)
        # else:
        #    self.last_regions[:-1] = self.last_regions[1:]
        #    self.last_regions[-1] = region
        # return region

    def decode_list(self, points) -> list:
        return copy.deepcopy([self.assign_region(p) for p in points])

    def is_outdated(self):
        if len(self.last_regions) > self.timeout_threshold:
            return all(self.last_regions) or not any(self.last_regions)
        else:
            return False


class PesteVector(Vector):
    def __init__(self, front_data: np.ndarray = 0, back_data: np.ndarray = 0, *args, **kwargs):
        super(PesteVector, self).__init__(*args, **kwargs)
        self.front_value = front_data
        self.back_value = back_data

    def get_data(self, other, value: np.ndarray = 0, return_region: bool = False) -> np.ndarray:
        region = super(PesteVector, self).assign_region(other=other)
        if region == 1:
            self.front_value = self.front_value + value
            return (self.front_value, region) if return_region else self.front_value
        else:

            self.back_value = self.back_value + value
            return (self.back_value, region) if return_region else self.back_value

    def assign_region(self, other: np.ndarray, value: float = 0.0) -> int:
        region = super(PesteVector, self).assign_region(other=other)
        if region == 0:
            self.back_value += value
        else:
            self.front_value += value
        return region

    def is_outdated(self):
        min_age = (self.front_value + self.back_value) > 2000
        proportion = min(self.front_value, self.back_value) / (
            1e-7 + max(self.front_value, self.back_value)
        )
        too_skewed = proportion < 0.1
        return min_age and too_skewed


def diversity_score(x, total=None):
    n_different_rows = np.unique(x, axis=0).shape[0]
    return n_different_rows if total is None else float(n_different_rows / total)


class __Encoder:
    def __init__(self, n_vectors: int, timeout: int = 1e100, timeout_threshold: int = 100):
        self.n_vectors = n_vectors
        self.timeout = timeout
        self.timeout_threshold = timeout_threshold
        self._vectors = []
        self._last_encoded = None

    @property
    def vectors(self):
        return self._vectors

    def __repr__(self):
        div_score = -1 if self._last_encoded is None else diversity_score(self._last_encoded, 1)
        den = float(self._last_encoded.shape[0]) if self._last_encoded is not None else 1.0
        text = (
            "Encoder with {} vectors, score {:.3f}, {} different hashes and {} "
            "available spaces\n".format(
                len(self),
                div_score / den,
                div_score,
                min(self.n_vectors - len(self), len(self.vectors)),
            )
        )
        return text  # + "".join(v.__repr__() for v in self.vectors)

    def __len__(self):
        return len(self._vectors)

    def __getitem__(self, item):
        return self.vectors[item]

    def reset(self):
        self._vectors = []

    def append(self, *args, **kwargs):
        kwargs["timeout"] = kwargs.get("timeout", self.timeout)
        kwargs["timeout_threshold"] = kwargs.get("timeout_threshold", self.timeout_threshold)
        vector = PesteVector(*args, **kwargs)
        self.append_vector(vector=vector)

    def append_vector(self, vector: Vector):
        if len(self) < self.n_vectors:
            self.vectors.append(vector)
        else:
            self.vectors[:-1] = copy.deepcopy(self.vectors[1:])
            self.vectors[-1] = vector

    def pct_different_hashes(self, points: np.ndarray) -> float:
        array = self.encode(points)
        return float(np.unique(array, axis=0).shape[0] / int(points.shape[0]))

    def is_valid_base(self, vector: [Vector, int], points: list):
        if isinstance(vector, int):
            vector = self[vector]
        binary = vector.decode_list(points)
        return not all(binary) and any(binary)

    def _apply_vectors_to_point(self, point, func_name: str, *args, **kwargs):
        values = np.array(
            [getattr(vector, func_name)(point, *args, **kwargs) for vector in self.vectors]
        )
        return values

    def get_pest(self, points) -> np.ndarray:
        values = np.vstack(
            [
                self._apply_vectors_to_point(point=points[i], func_name="get_data", value=1)
                for i in range(points.shape[0])
            ]
        )
        return values

    def encode(self, points):
        values = np.vstack(
            [
                self._apply_vectors_to_point(point=points[i], func_name="assign_region")
                for i in range(points.shape[0])
            ]
        )
        self._last_encoded = values
        return values

    def remove_duplicates(self):
        self._vectors = list(set(self.vectors))

    def remove_bases(self, points):
        self._vectors = [
            v
            for v in self.vectors
            if not v.is_outdated() and self.is_valid_base(vector=v, points=points)
        ]
        # self._vectors = [v for v in self.vectors if self.is_valid_base(vector=v, points=points)]
        self.remove_duplicates()

    def update_bases(self, vectors):
        n_vec = len(vectors)
        available_spaces = min(self.n_vectors - len(self), n_vec)

        if available_spaces > 0:
            chosen_vectors = np.random.choice(np.arange(n_vec), available_spaces, replace=False)
            for ix in chosen_vectors:
                origin, end = vectors[ix]
                vec = PesteVector(origin=origin.copy(), end=end.copy(), timeout=self.timeout)
                self.append_vector(vec)

    def get_bases(self) -> np.ndarray:
        return np.vstack([v.base.copy() for v in self.vectors]).copy()
