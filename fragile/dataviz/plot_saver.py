import os

import holoviews

from fragile.core import SwarmWrapper
from fragile.core.states import OneWalker, StatesEnv, StatesModel, StatesWalkers
from fragile.dataviz.swarm_viz import SwarmViz


class PlotSaver(SwarmWrapper):
    """
    Save the plots generated by a swarm to the target path.
    """

    def __init__(self, swarm: SwarmViz, output_path: str, fmt: str = "png", **kwargs):
        """
        Initialize a :class:`PlotSaver`.

        The available output formats depend on the backend being used. By
        default and if the filename is a string the output format will be
        inferred from the file extension. Otherwise an explicit format
        will need to be specified. For ambiguous file extensions such as
        html it may be necessary to specify an explicit fmt to override
        the default, e.g. in the case of 'html' output the widgets will
        default to fmt='widgets', which may be changed to scrubber widgets
        using fmt='scrubber'.

        Args:
            swarm: The :class:`SwarmViz` producing the plots that will be exported.
            output_path: Path where the plots will be exported.
            fmt: The format to save the object as, e.g. png, svg, html, or gif
                and if widgets are desired either 'widgets' or 'scrubber'
            **kwargs: dict
                Additional keyword arguments passed to the renderer,
                e.g. fps for animations

        """
        if not isinstance(swarm, SwarmViz):
            raise TypeError("swarm must be an instance of SwarmViz. Got %s instead" % type(swarm))
        super(PlotSaver, self).__init__(swarm, name="__swarm")
        # self._swarm_viz = swarm
        self.output_path = output_path
        self._save_kwargs = kwargs
        self._fmt = fmt
        self.plot()

    def run_step(self):
        """
        Compute one iteration of the :class:`Swarm` evolution process and \
        update all the data structures, and stream the data to the created plots.
        """
        self.unwrapped.run_step()
        if self.unwrapped.epoch % self.stream_interval == 0:
            self.stream_plots()
            self.save_plot()

    def _get_file_name(self) -> str:
        swarmviz_name = self.unwrapped.__class__.__name__.lower()
        filename = "%s_%05d.%s" % (swarmviz_name, self.swarm.epoch, self._fmt)
        return filename

    def save_plot(self):
        """Save the plot of the wrapped :class:`Swarm` to the target path."""
        filename = self._get_file_name()
        filepath = os.path.join(self.output_path, filename)
        holoviews.save(
            self.current_plot,
            filename=filepath,
            fmt=self._fmt,
            **self._save_kwargs,
            backend=holoviews.Store.current_backend,
        )

    def run(
        self,
        root_walker: OneWalker = None,
        env_states: StatesEnv = None,
        model_states: StatesModel = None,
        walkers_states: StatesWalkers = None,
        report_interval: int = None,
        show_pbar: bool = None,
    ):
        """
        Run a new search process.

        Args:
            root_walker: Walker representing the initial state of the search. \
                         The walkers will be reset to this walker, and it will \
                         be added to the root of the :class:`StateTree` if any.
            env_states: :class:`StatesEnv` that define the initial state of the model.
            model_states: :class:`StatesEModel that define the initial state of the environment.
            walkers_states: :class:`StatesWalkers` that define the internal states of the walkers.
            report_interval: Display the algorithm progress every ``report_interval`` epochs.
            show_pbar: A progress bar will display the progress of the algorithm run.
        Returns:
            None.

        """
        self.unwrapped.__class__.run(self)
