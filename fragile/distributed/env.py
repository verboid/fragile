import atexit
import multiprocessing
import sys
import traceback
from typing import Callable, Dict, List, Tuple, Union

from fragile.backend import tensor, typing  # noqa: F401
from fragile.core.env import Environment as CoreEnv
from fragile.core.states import StatesEnv, StatesModel
from fragile.core.utils import split_args_in_chunks, split_kwargs_in_chunks
from fragile.core.wrappers import BaseWrapper, EnvWrapper
from fragile.distributed.ray import ray


class _ExternalProcess:
    """
    Step environment in a separate process for lock free paralellism.

    The environment will be created in the external process by calling the
    specified callable. This can be an environment class, or a function
    creating the environment and potentially wrapping it. The returned
    environment should not access global variables.

    Args:
      constructor: Callable that creates and returns an OpenAI gym environment.

    Attributes:
        TARGET: Name of the function that will be applied.

    ..notes:
        This is mostly a copy paste from
        https://github.com/tensorflow/agents/blob/master/agents/tools/wrappers.py,
        but it lets us set and read the environment state.

    """

    # Message types for communication via the pipe.
    _ACCESS = 1
    _CALL = 2
    _RESULT = 3
    _EXCEPTION = 4
    _CLOSE = 5
    TARGET = "make_transitions"

    def __init__(self, constructor):

        self._conn, conn = multiprocessing.Pipe()
        self._process = multiprocessing.Process(target=self._worker, args=(constructor, conn))
        atexit.register(self.close)
        self._process.start()
        self._states_shape = None
        self._observs_shape = None

    def __getattr__(self, name):
        """
        Request an attribute from the environment.

        Note that this involves communication with the external process, so it can
        be slow.

        Args:
          name: Attribute to access.

        Returns:
          Value of the attribute.

        """
        self._conn.send((self._ACCESS, name))
        return self._receive()

    def call(self, name, *args, **kwargs):
        """
        Asynchronously call a method of the external environment.

        Args:
          name: Name of the method to call.
          *args: Positional arguments to forward to the method.
          **kwargs: Keyword arguments to forward to the method.

        Returns:
          Promise object that blocks and provides the return value when called.

        """
        payload = name, args, kwargs
        self._conn.send((self._CALL, payload))
        return self._receive

    def close(self):
        """Send a close message to the external process and join it."""
        try:
            self._conn.send((self._CLOSE, None))
            self._conn.close()
        except IOError:
            # The connection was already closed.
            pass
        self._process.join()

    def make_transitions(self, blocking: bool = False, *args, **kwargs):
        """
        Vectorized version of the ``TARGET`` method.

        Args:
           blocking: If True, execute sequentially.
           args: Passed tot he target function.
           kwargs: passed to the target function.

        Returns:
            Return values of the target function.

        """
        promise = self.call(self.TARGET, *args, **kwargs)
        if blocking:
            return promise()
        else:
            return promise

    def execute(self, name, blocking: bool = False, *args, **kwargs):
        promise = self.call(name, *args, **kwargs)
        if blocking:
            return promise()
        else:
            return promise

    def reset(self, blocking: bool = False, *args, **kwargs):
        """
        Reset the internal environment.

        Args:
           blocking: If True, execute sequentially.
           args: Passed tot he target function.
           kwargs: passed to the target function.

        Returns:
            Return values of the target function.

        """
        promise = self.call("reset", *args, **kwargs)
        if blocking:
            return promise()
        else:
            return promise

    def _receive(self):
        """
        Wait for a message from the worker process and return its payload.

        Raises:
          Exception: An exception was raised inside the worker process.
          KeyError: The received message is of an unknown type.

        Returns:
          Payload object of the message.

        """
        message, payload = self._conn.recv()
        # Re-raise exceptions in the main process.
        if message == self._EXCEPTION:
            stacktrace = payload
            raise Exception(stacktrace)
        if message == self._RESULT:
            return payload
        raise KeyError("Received message of unexpected type {}".format(message))

    def _worker(self, constructor, conn):
        """
        Wait for input data and sends back environment results.

        Args:
          constructor: Constructor for the OpenAI Gym environment.
          conn: Connection for communication to the main process.

        Raises:
          KeyError: When receiving a message of unknown type.

        """
        try:
            env = constructor()
            while True:
                try:
                    # Only block for short times to have keyboard exceptions be raised.
                    if not conn.poll(0.1):
                        continue
                    message, payload = conn.recv()
                except (EOFError, KeyboardInterrupt):
                    break
                if message == self._ACCESS:
                    name = payload
                    result = getattr(env, name)
                    conn.send((self._RESULT, result))
                    continue
                if message == self._CALL:
                    name, args, kwargs = payload
                    result = getattr(env, name)(*args, **kwargs)
                    conn.send((self._RESULT, result))
                    continue
                if message == self._CLOSE:
                    assert payload is None
                    break
                raise KeyError("Received message of unknown type {}".format(message))
        except Exception:  # pylint: disable=broad-except
            stacktrace = "".join(traceback.format_exception(*sys.exc_info()))
            conn.send((self._EXCEPTION, stacktrace))
            conn.close()


class _BatchEnv:
    """
    Combine multiple environments to make_transitions in batch.

    It is mostly a copy paste from \
    https://github.com/tensorflow/agents/blob/master/agents/tools/wrappers.py \
    that also allows to set and get the states.

    To make_transitions environments in parallel, environments must support a \
    `blocking=False` argument to their make_transitions and reset functions that makes them \
    return callables instead to receive the result at a later time.

    Args:
      envs: List of environments.
      blocking: Step environments after another rather than in parallel.

    Raises:
      ValueError: Environments have different observation or action spaces.

    """

    def __init__(self, envs, blocking):
        self._envs = envs
        self._n_chunks = len(self._envs)
        self._blocking = blocking
        self.kwargs_mode = None

    def __len__(self):
        """Return mumber of combined environments."""
        return len(self._envs)

    def __getitem__(self, index):
        """Access an underlying environment by index."""
        return self._envs[index]

    def __getattr__(self, name):
        """
        Forward unimplemented attributes to one of the original environments.

        Args:
          name: Attribute that was accessed.

        Returns:
          Value behind the attribute name one of the wrapped environments.

        """
        return getattr(self._envs[0], name)

    def close(self):
        """Send close messages to the external process and join them."""
        for env in self._envs:
            if hasattr(env, "close"):
                env.close()

    def reset(self, batch_size: int = 1, env_states: StatesEnv = None, **kwargs) -> StatesEnv:
        results = [
            env.reset(self._blocking, batch_size=batch_size, env_states=env_states, **kwargs)
            for env in self._envs
        ]
        states = [result if self._blocking else result() for result in results]
        return states[0]

    def make_transitions(self, *args, **kwargs):
        """Use the underlying parallel environment to calculate the state transitions."""
        chunk_data = self._split_inputs_in_chunks(*args, **kwargs)
        split_results = self._make_transitions(chunk_data)

        merged = self._merge_data(split_results)
        return merged

    @staticmethod
    def _merge_data(data_dicts: List[Dict[str, typing.Tensor]]):
        kwargs = {}
        for k in data_dicts[0].keys():
            try:
                grouped = tensor.concatenate([tensor.to_backend(ddict[k]) for ddict in data_dicts])
            except Exception:
                val = str([ddict[k].shape for ddict in data_dicts])
                raise ValueError(val)
            kwargs[k] = grouped
        return kwargs

    def _split_inputs_in_chunks(self, *args, **kwargs):
        self.kwargs_mode = len(args) == 0
        if self.kwargs_mode:
            return split_kwargs_in_chunks(kwargs, self._n_chunks)
        else:
            return split_args_in_chunks(args, self._n_chunks)

    def _make_transitions(self, split_results):
        results = [
            env.make_transitions(self._blocking, **chunk)
            if self.kwargs_mode
            else env.make_transitions(self._blocking, *chunk)
            for env, chunk in zip(self._envs, split_results)
        ]
        data_dicts = [result if self._blocking else result() for result in results]
        return data_dicts

    def distribute(self, name, **kwargs):
        chunk_data = self._split_inputs_in_chunks(**kwargs)
        results = [
            env.execute(name=name, blocking=self._blocking, **chunk)
            for env, chunk in zip(self._envs, chunk_data)
        ]
        split_results = [result if self._blocking else result() for result in results]
        if isinstance(split_results[0], dict):
            merged = self._merge_data(split_results)
        else:  # Assumes batch of tensors
            split_results = [tensor.to_backend(res) for res in split_results]
            merged = tensor.concatenate(split_results)
        return merged


class _ParallelEnvironment:
    """Wrap any environment to be stepped in parallel when step is called."""

    def __init__(self, env_callable, n_workers: int = 8, blocking: bool = False):
        self._env = env_callable()
        envs = [_ExternalProcess(constructor=env_callable) for _ in range(n_workers)]
        self._batch_env = _BatchEnv(envs, blocking)

    def __getattr__(self, item):
        return getattr(self._env, item)

    def close(self):
        """Close Environment processes."""
        for env in self._batch_env._envs:
            env.close()

    def make_transitions(self, *args, **kwargs) -> Dict[str, typing.Tensor]:
        """Use the underlying parallel environment to calculate the state transitions."""
        return self._batch_env.make_transitions(*args, **kwargs)

    def distribute(self, name, **kwargs):
        return self._batch_env.distribute(name, **kwargs)

    def reset(self, batch_size: int = 1, **kwargs) -> StatesEnv:
        """
        Reset the :class:`Environment` to the start of a new episode and returns \
        an :class:`StatesEnv` instance describing its internal state.

        Args:
            batch_size: Number of walkers that the returned state will have.
            **kwargs: Ignored. This environment resets without using any external data.

        Returns:
            :class:`EnvStates` instance describing the state of the :class:`Environment`. \
            The first dimension of the data tensors (number of walkers) will be \
            equal to batch_size.

        """
        return self._batch_env.reset(batch_size=batch_size, **kwargs)


class ParallelEnv(EnvWrapper):
    """
    Make the transitions of an :class:`Environment` in parallel using the \
    multiprocessing library.
    """

    def __init__(
        self,
        env_callable: Callable[..., CoreEnv],
        n_workers: int = 8,
        blocking: bool = False,
        distribute: List[str] = None,
    ):
        """
        Initialize a :class:`ParallelEnv`.

        Args:
            env_callable: Returns the :class:`Environment` that will be distributed.
            n_workers: Number of processes that will step the \
                       :class:`Environment` in parallel.
            blocking: If ``True`` perform the steps in a sequential fashion and \
                      block the process between steps.
            distribute: List of function names that will be executed in all the different workers.

        """
        self.n_workers = n_workers
        self.blocking = blocking
        self._distribute_names = distribute if distribute is not None else []
        self.parallel_env = _ParallelEnvironment(
            env_callable=env_callable, n_workers=n_workers, blocking=blocking
        )
        super(ParallelEnv, self).__init__(env_callable(), name="_local_env")

    def __getattr__(self, item):
        if item in self._distribute_names:
            return lambda **kwargs: self.distribute(item, **kwargs)
        elif isinstance(self._local_env, BaseWrapper):
            return getattr(self._local_env, item)
        return self._local_env.__getattribute__(item)

    def __call__(self, *args, **kwargs) -> "ParallelEnv":
        """
        Return the current instance of :class:`BaseEnvironment`.

        This is used to avoid defining a ``environment_callable `` as \
        ``lambda: environment_instance`` when initializing a :class:`Swarm`. If the \
        :class:`Environment` needs is passed to a remote process, you may need \
        to write custom serialization for it, or resort to creating an appropriate \
        ``environment_callable``.
        """
        return self

    def close(self):
        """Close the processes created by the internal parallel_environment."""
        return self.parallel_env.close()

    def step(self, model_states: StatesModel, env_states: StatesEnv) -> StatesEnv:
        """
        Set the environment to the target and perform an step in parallel.

        Args:
            model_states: States corresponding to the model data.
            env_states: States class containing the state data to be set on the Environment.

        Returns:
            States containing the information that describes the new state of the Environment.

        """
        transition_data = self.states_to_data(model_states=model_states, env_states=env_states)
        if not isinstance(transition_data, (dict, tuple)):
            raise ValueError(
                "The returned values from states_to_data need to "
                "be an instance of dict or tuple. "
                "Got %s instead" % type(transition_data)
            )

        new_data = (
            self.parallel_env.make_transitions(*transition_data)
            if isinstance(transition_data, tuple)
            else self.parallel_env.make_transitions(**transition_data)
        )
        new_env_state = self.states_from_data(len(env_states), **new_data)
        return new_env_state

    def distribute(self, name, **kwargs):
        """Execute the target function in all the different workers."""
        return self.parallel_env.distribute(name, **kwargs)

    def states_to_data(
        self, model_states: StatesModel, env_states: StatesEnv
    ) -> Union[Dict[str, typing.Tensor], Tuple[typing.Tensor, ...]]:
        """Use the wrapped environment to get the data with no parallelization."""
        return self._local_env.states_to_data(model_states=model_states, env_states=env_states)

    def states_from_data(self, batch_size: int, *args, **kwargs) -> StatesEnv:
        """Use the wrapped environment to create the states with no parallelization."""
        return self._local_env.states_from_data(batch_size=batch_size, *args, **kwargs)

    def reset(self, batch_size: int = 1, env_states: StatesEnv = None, **kwargs) -> StatesEnv:
        """
        Reset the environment and return :class:`StatesEnv` class with batch_size copies \
        of the initial state.

        Args:
            batch_size: Number of walkers that the resulting state will have.
            env_states: States class used to set the environment to an arbitrary \
                        state.
            kwargs: Additional keyword arguments not related to environment data.

        Returns:
            States class containing the information of the environment after the \
             reset.

        """
        states = self._local_env.reset(batch_size=batch_size, env_states=env_states, **kwargs)
        return states


class RayEnv(EnvWrapper):
    """Step an :class:`Environment` in parallel using ``ray``."""

    def __init__(
        self,
        env_callable: Callable[[dict], CoreEnv],
        n_workers: int,
        env_kwargs: dict = None,
        options: dict = None,
        distribute: List[str] = None,
    ):
        """
        Initialize a :class:`RayEnv`.

        Args:
            env_callable: Returns the :class:`Environment` that will be distributed.
            n_workers: Number of processes that will step the \
                       :class:`Environment` in parallel.
            env_kwargs: Passed to ``env_callable``.
            options: passed to RemoteEnvironment.options().
            distribute: List of function names that will be executed in all the different workers.

        """
        from fragile.distributed.ray.env import Environment as RemoteEnvironment

        options = options if options is not None else {}
        env_kwargs = {} if env_kwargs is None else env_kwargs
        self.n_workers = n_workers
        self._distribute_name = distribute if distribute is not None else {}
        self.envs: List[RemoteEnvironment] = [
            RemoteEnvironment.options(**options).remote(
                env_callable=env_callable, env_kwargs=env_kwargs
            )
            for _ in range(n_workers)
        ]
        super(RayEnv, self).__init__(env_callable(), name="_local_env")

    def __call__(self, *args, **kwargs) -> "RayEnv":
        """
        Return the current instance of :class:`BaseEnvironment`.

        This is used to avoid defining a ``environment_callable `` as \
        ``lambda: environment_instance`` when initializing a :class:`Swarm`. If the \
        :class:`Environment` needs is passed to a remote process, you may need \
        to write custom serialization for it, or resort to creating an appropriate \
        ``environment_callable``.
        """
        return self

    def __getattr__(self, item):
        if item in self._distribute_name:
            return lambda **kwargs: self.distribute(name=item, **kwargs)
        if isinstance(self._local_env, BaseWrapper):
            return getattr(self._local_env, item)
        return self._local_env.__getattribute__(item)

    def step(self, model_states: StatesModel, env_states: StatesEnv) -> StatesEnv:
        """
        Set the environment to the target states by applying the specified \
        actions an arbitrary number of time steps.

        The state transitions will be calculated in parallel.

        Args:
            model_states: :class:`StatesModel` representing the data to be used \
                         to act on the environment.
            env_states: :class:`StatesEnv` representing the data to be set in \
                        the environment.

        Returns:
            :class:`StatesEnv` containing the information that describes the \
            new state of the Environment.

        """
        transition_data = self._local_env.states_to_data(
            model_states=model_states, env_states=env_states
        )
        if not isinstance(transition_data, (dict, tuple)):
            raise ValueError(
                "The returned values from states_to_data need to "
                "be an instance of dict or tuple. "
                "Got %s instead" % type(transition_data)
            )
        new_data = (
            self.make_transitions(*transition_data)
            if isinstance(transition_data, tuple)
            else self.make_transitions(**transition_data)
        )
        new_env_state = self._local_env.states_from_data(len(env_states), **new_data)
        return new_env_state

    def make_transitions(self, *args, **kwargs):
        """
        Forward the make_transitions arguments to the parallel environments \
        splitting them in batches of similar size.
        """
        chunk_data = self._split_inputs_in_chunks(*args, **kwargs)
        split_results = self._make_transitions(chunk_data)
        merged = self._merge_data(split_results)
        return merged

    @staticmethod
    def _merge_data(data_dicts: List[Dict[str, typing.Tensor]]):
        kwargs = {}
        for k in data_dicts[0].keys():
            grouped = tensor.concatenate([ddict[k] for ddict in data_dicts])
            kwargs[k] = grouped
        return kwargs

    def _split_inputs_in_chunks(self, *args, **kwargs):
        self.kwargs_mode = len(args) == 0
        if self.kwargs_mode:

            return split_kwargs_in_chunks(kwargs, len(self.envs))
        else:
            return split_args_in_chunks(args, len(self.envs))

    def _make_transitions(self, chunk_data):
        from fragile.distributed.ray import ray

        results = [
            env.make_transitions.remote(**chunk)
            if self.kwargs_mode
            else env.make_transitions.remote(*chunk)
            for env, chunk in zip(self.envs, chunk_data)
        ]
        data_dicts = ray.get(results)
        return data_dicts

    def distribute(self, name, **kwargs):
        """Execute the target function in all the different workers."""
        chunk_data = self._split_inputs_in_chunks(**kwargs)
        from fragile.distributed.ray import ray

        results = [
            env.execute.remote(name=name, **chunk) for env, chunk in zip(self.envs, chunk_data)
        ]
        split_results = ray.get(results)
        if isinstance(split_results[0], dict):
            merged = self._merge_data(split_results)
        else:  # Assumes batch of tensors
            split_results = [tensor.to_backend(res) for res in split_results]
            merged = tensor.concatenate(split_results)
        return merged

    def reset(
        self, batch_size: int = 1, env_states: StatesEnv = None, *args, **kwargs
    ) -> StatesEnv:
        """
        Reset the environment to the start of a new episode and returns a new \
        States instance describing the state of the Environment.

        Args:
            batch_size: Number of walkers that the returned state will have.
            env_states: :class:`StatesEnv` representing the data to be set in \
                        the environment.
            *args: Passed to the internal environment ``reset``.
            **kwargs: Passed to the internal environment ``reset``.

        Returns:
            States instance describing the state of the Environment. The first \
            dimension of the data tensors (number of walkers) will be equal to \
            batch_size.

        """
        reset = [
            env.reset.remote(batch_size=batch_size, env_states=env_states, *args, **kwargs)
            for env in self.envs
        ]
        ray.get(reset)
        return self._local_env.reset(batch_size=batch_size, env_states=env_states, *args, **kwargs)
