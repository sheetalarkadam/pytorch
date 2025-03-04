import argparse
from typing import Any, Callable, Tuple, Dict, Optional
import logging

import torch
import torch.fx
from torch.fx.node import map_arg

from .shape_prop import ShapeProp
from .split_utils import split_by_tags
from .tools_common import (
    Tensors,
    TensorOrTensors,
    NodeList,
    NodeSet,
    CALLABLE_NODE_OPS,
    FxNetAccFusionsFinder,
    Names
)

_LOGGER = logging.getLogger(__name__)


class FxNetMinimizerBadModuleError(Exception):
    """
    Raised if failed to split out a minimize module
    """

    pass


class FxNetMinimizerRunFuncError(Exception):
    """
    Raised if error occurs during run_a or run_b functions
    """

    pass


class FxNetMinimizerResultMismatchError(Exception):
    """
    Raised if comparing function thinks the results are mismatching.
    """

    pass


class _MinimizerSettingBase:
    def __init__(self):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--accumulate_error",
            default=False,
            action="store_true",
            help="Instead of using a's input for both converted module to verify, use the "
            "previous outputs of each converted module as input to accumulate the errors.",
        )

        parser.add_argument(
            "--traverse_method",
            default="sequential",
            choices=["sequential", "binary", "accumulate"],
            help="Determine the way of traverse the nodes in FX module.",
        )
        parser.add_argument(
            "--find_all",
            default=False,
            action="store_true",
            help="Minimizer will go through the entire model and return all problematic nodes.",
        )
        parser.add_argument(
            "--return_intermediate",
            default=False,
            action="store_true",
            help="If true, when using `run_nodes()` function to run the model, intermediate results "
            "of all the ops will be returned as output.",
        )
        args, unknown = parser.parse_known_args()

        self.accumulate_error: bool = args.accumulate_error
        self.traverse_method: str = args.traverse_method
        self.find_all: bool = args.find_all
        self.return_intermediate: bool = args.return_intermediate

    def __str__(self):
        settings_str = "FX Minimizer Settings:\n"

        for k, v in vars(self).items():
            settings_str += f"\t{k}: {v}\n"

        return settings_str


class _MinimizerBase:
    """
    This class is used to automatically find problematic nodes in a model. It takes a FX
    graphmodule and generate some submodules while traverse the graph. Then two functions
    `run_a` and `run_b` will be used to run the same submodule and a function `compare_fn`
    will be used to compare the results.

    Currently we provides two ways to traverse the graph and generate submodules.
        1. Sequential traversal: this will traverse the graph node by node and generate
           one submodule with one sigle node.
        2. Binary searching: this will do a binary search style traversal on the graph.

    For internal Users, a guide can be found here https://fb.quip.com/HDtuAgiKGfkP.
    """

    def __init__(
        self,
        module: torch.fx.GraphModule,
        sample_input: Tensors,
        compare_fn: Callable[[TensorOrTensors, TensorOrTensors, Names], Tuple[float, bool]],
        settings: _MinimizerSettingBase,
    ):
        assert isinstance(module, torch.fx.GraphModule)

        self.module = module
        self.sample_input = sample_input
        self.compare_fn = compare_fn
        self.settings = settings

        # Stores outputs of run_a function
        self.a_outputs: Dict[str, Any] = {}

        # Stores outputs of run_b function
        self.b_outputs: Dict[str, Any] = {}

        # Stores the results of compare_fn
        self.results: Dict[Any, Any] = {}

        callable_nodes = {
            node for node in self.module.graph.nodes if node.op in CALLABLE_NODE_OPS
        }
        ShapeProp(self.module).propagate(*self.sample_input)
        self.fusions = FxNetAccFusionsFinder(self.module, callable_nodes)()

        # Check if number of input in sample_input matches the number of placeholders
        placeholders = [
            node.name for node in self.module.graph.nodes if node.op == "placeholder"
        ]
        assert len(placeholders) == len(self.sample_input)

        # Store sample_input
        for i, name in enumerate(placeholders):
            self.a_outputs[name] = sample_input[i]
            self.b_outputs[name] = sample_input[i]

    def run_a(self, mod: torch.fx.GraphModule, inputs: Tensors) -> TensorOrTensors:
        """
        Run `mod` with `inputs` and generate output. The output will be compared with
        output of run_b().
        """
        raise RuntimeError("run_a() is not implemented.")

    def run_b(self, mod: torch.fx.GraphModule, inputs: Tensors) -> TensorOrTensors:
        """
        Run `mod` with `inputs` and generate output. The output will be compared with
        output of run_a().
        """
        raise RuntimeError("run_b() is not implemented.")

    def _store_outputs(
        self,
        a_result: TensorOrTensors,
        b_result: TensorOrTensors,
        submodule: torch.fx.GraphModule,
    ):
        """
        Store the outputs of self.run_a() and self.run_b() into self.a_outputs and
        self.b_outputs, so that we can use them when execute preceding nodes that
        use those outputs as inputs.

        Args:
            a_result: Output of self.run_a(). Could be a tensor or tensors.
            b_result: Output of self.run_b(). Could be a tensor or tensors.
            submodule: The module that generates a_result and b_result.
        """
        output_node = next(
            node for node in submodule.graph.nodes if node.op == "output"
        )

        # Only one output
        if isinstance(output_node.args[0], torch.fx.Node):
            self.a_outputs[output_node.args[0].name] = a_result
            self.b_outputs[output_node.args[0].name] = b_result
        # Multiple outputs
        else:
            for i, arg in enumerate(output_node.args[0]):
                self.a_outputs[arg.name] = a_result[i]
                self.b_outputs[arg.name] = b_result[i]

    def _get_submod_inputs(
        self, main_module: torch.fx.GraphModule, submod_path: str
    ) -> Tuple[Tensors, Tensors]:
        """
        Try get submodule inputs from stored outputs. If not found then use
        torch_glow.get_submod_inputs to get the inputs.

        If accumulate_error is False, use a_input for run_a() and run_b()
        otherwise use a_input for run_a and b_input for run_b.

        Args:
            main_module: Top-levlel fx module.
            submod_path: Path to the submodule we want to run and compare results.

        Returns:
            a_input: List of tensor(s) that will be used by run_a() as submodule inputs.
            b_input: List of tensor(s) that will be used by run_b() as submodule inputs.
        """
        a_input = []
        b_input = []
        submodule = getattr(main_module, submod_path)
        placeholders = [
            node.name for node in submodule.graph.nodes if node.op == "placeholder"
        ]

        # If all placeholder can be found in stored outputs, use stored
        # outputs as inputs. Otherwise, use `torch_glow.get_submod_inputs`
        # to get the inputs.
        if set(placeholders) <= self.a_outputs.keys():
            for name in placeholders:
                a_input.append(self.a_outputs[name])
                b_input.append(self.b_outputs[name])
        else:
            if self.settings.accumulate_error:
                print(f"Can't find previous stored outputs named {placeholders}!")

            def get_inputs(self: torch.nn.Module, inputs: Any):
                nonlocal a_input
                a_input = inputs

            # Use forward hook to get the inputs to the submodule
            handle = submodule.register_forward_pre_hook(get_inputs)
            main_module(*self.sample_input)
            handle.remove()

            b_input = a_input

        if not self.settings.accumulate_error:
            return a_input, a_input

        return a_input, b_input

    def _tag_nodes(self, selected_nodes: NodeSet):
        """
        Tag selected nodes with tag "minimize". Nodes with the same tags will
        be split to the same submodule afterwards.

        Args:
            selected_nodes: Nodes that we want to minimize. We will tag those nodes
                with "minimize", all preceding nodes with "main_0" and all following
                nodes with "main_1".
        """
        for node in self.module.graph.nodes:
            if node.op not in CALLABLE_NODE_OPS:
                continue

            if node in selected_nodes:
                node.tag = "minimize"
            elif any(
                n.tag in {"minimize", "main_1"}
                for n in node.all_input_nodes
                if n.op in CALLABLE_NODE_OPS
            ):
                node.tag = "main_1"
            else:
                node.tag = "main_0"

    def _build_submodule(self, nodes: NodeSet) -> Tuple[torch.fx.GraphModule, str]:
        """
        Split self.module so that one submodule consists of `nodes` and only `nodes`.

        Args:
            nodes: Nodes that we want to include in the minimize submodule.

        Returns:
            split_module (torch.fx.GraphModule): the module after split.
            submodule_name (str): the name of the submodule that consists of `nodes`.
        """
        # Color provided nodes
        self._tag_nodes(nodes)

        # Split module based on coloring
        split_module = split_by_tags(self.module, ["main_0", "minimize", "main_1"])

        # Find submodule containing colored nodes
        submodule_name: str = ""
        for child_name, _ in split_module.named_children():
            # Skip submodules we're not interested in at the moment
            if "minimize" not in child_name:
                continue

            if submodule_name == "":
                submodule_name = child_name
            else:
                raise FxNetMinimizerBadModuleError(
                    f"Expected only one minimize submodule with nodes {nodes}"
                )

        if submodule_name == "":
            raise FxNetMinimizerBadModuleError(
                f"Minimize submodule was not found with nodes {nodes}"
            )

        return split_module, submodule_name

    def _run_and_compare(
        self,
        split_module: torch.fx.GraphModule,
        submod_name: str,
        output_names: Names
    ):
        """
        Run the submodule in `split_module` that has name `submod_name`
        using `self.run_a` and `self.run_b` and compare their results.

        Args:
            split_module: Main module that contains the minimize submodule.
            submod_name: Name of the minimize submodule.
            output_names: Names of the node we want to output. If None, we
                will use the original output.
        """
        submodule = getattr(split_module, submod_name)
        a_input, b_input = self._get_submod_inputs(split_module, submod_name)

        if output_names:
            output_nodes: NodeList = []
            for node in submodule.graph.nodes:
                if node.op == "output":
                    submodule.graph.erase_node(node)

                if node.name in output_names:
                    output_nodes.append(node)

            submodule.graph.output(
                output_nodes[0] if len(output_nodes) == 1 else tuple(output_nodes)
            )
            submodule.graph.lint()
            submodule.recompile()

        # Use name of args in output node as key to store comparison result
        for node in submodule.graph.nodes:
            if node.op == "output":
                result_key = map_arg(node.args, lambda x: x.name)

        a_result = self.run_a(submodule, a_input)
        b_result = self.run_b(submodule, b_input)
        self._store_outputs(a_result, b_result, submodule)

        # Compare results
        names: Names = output_names
        if output_names is None:
            names = [str(v) for v in result_key]
        numeric_result, bool_result = self.compare_fn(a_result, b_result, names)
        self.results[result_key] = numeric_result
        if not bool_result:
            raise FxNetMinimizerResultMismatchError(f"Result mismatch for {result_key}")

    def _binary_search_impl(self, nodes: NodeList) -> NodeSet:
        """
        Recursive binary search implementation.
        """
        cur_nodes: NodeSet = set(nodes)
        for node in nodes:
            if node in self.fusions:
                cur_nodes.update(self.fusions[node])

        try:
            split_module, submod_name = self._build_submodule(cur_nodes)
            self._run_and_compare(
                split_module,
                submod_name,
                []
            )
        except (FxNetMinimizerRunFuncError, FxNetMinimizerResultMismatchError):
            if len(nodes) == 1:
                return cur_nodes

            mid = len(nodes) // 2

            culprits = self._binary_search_impl(nodes[:mid])
            if not self.settings.find_all:
                return culprits

            culprits.update(self._binary_search_impl(nodes[mid:]))
            if len(culprits) == 0:
                raise FxNetMinimizerBadModuleError(
                    "Found an error in a group of nodes, but was not able to minimize",
                    nodes,
                )
            return culprits
        else:
            return set()

    def _binary_traverse(self, nodes: NodeList) -> NodeSet:
        """
        Binary search on `nodes` for culprit.
        """
        return self._binary_search_impl(nodes)

    def _sequential_traverse(self, nodes: NodeList) -> NodeSet:
        """
        Traverse `nodes` one by one and determine if any of them is a culprit.
        """
        culprits: NodeSet = set()

        for node in nodes:
            _LOGGER.info(f"Visit node: {node.name}")
            cur_nodes: NodeSet = {node}

            if node in self.fusions:
                cur_nodes = self.fusions[node]

            try:
                split_module, submod_name = self._build_submodule(cur_nodes)
                self._run_and_compare(
                    split_module, submod_name, [node.name]
                )
            except (FxNetMinimizerResultMismatchError):
                culprits.add(node)
                if not self.settings.find_all:
                    return culprits
            except (FxNetMinimizerRunFuncError):
                culprits.update(cur_nodes)
                if not self.settings.find_all:
                    return culprits

        return culprits

    def _accumulate_traverse(self, nodes: NodeList) -> NodeSet:
        culprits: NodeSet = set()
        nodes_to_run: NodeSet = set()

        # find_all is not supported for accumulate traversal because all the
        # ops run on NNPI. So we return after the first op that raises error.
        if self.settings.find_all:
            print("'Find All' mode is not supported in accumulate traversal.")
            return culprits

        for node in nodes:
            nodes_to_run.add(node)

            node_name = node.name
            if node_name is not None and isinstance(node_name, tuple):
                node_name = node_name[0]
            assert node_name is not None and isinstance(node_name, str), f"minimize: node_name: {node_name}"

            try:
                split_module, submod_name = self._build_submodule(nodes_to_run)
                self._run_and_compare(split_module, submod_name, [node_name])
            except (FxNetMinimizerResultMismatchError,
                    FxNetMinimizerRunFuncError):
                culprits.add(node)
                return culprits

        return culprits

    def _collect_nodes(self, start: Optional[str], end: Optional[str]) -> NodeList:
        """
        Collect nodes in the model that between nodes with name of `start` and `end`.
        These two nodes are also included.
        """
        nodes: NodeList = []
        add_node = start is None

        for node in self.module.graph.nodes:
            if node.op not in CALLABLE_NODE_OPS:
                continue

            if node.name == start:
                add_node = True

            if add_node:
                nodes.append(node)

            if node.name == end:
                break

        return nodes

    def run_nodes(self, start: Optional[str] = None, end: Optional[str] = None):
        """
        Run part of the model from `start` node to `end` node. If `start` is None
        then we start from the beginning of the model. If `end` is None then we
        stop at the end of the model.

        Args:
            start: The name of the node which is the first node of the submodule
                we want to run. If set to None, then we'll start with the first
                node of the model.
            end: The name of the node which is the last node of the submodule we
                want to run. If set to None, we'll end with the last node of the
                model.
        """
        nodes = self._collect_nodes(start, end)
        cur_nodes = set(nodes)

        for node in nodes:
            if node in self.fusions:
                cur_nodes.update(self.fusions[node])

        output_names = []
        if self.settings.return_intermediate:
            output_names = [node.name for node in nodes]

        try:
            split_module, submod_name = self._build_submodule(cur_nodes)
            self._run_and_compare(split_module, submod_name, output_names)
        except (
            FxNetMinimizerRunFuncError,
            FxNetMinimizerResultMismatchError,
        ) as e:
            print(e)

    def minimize(self, start: Optional[str] = None, end: Optional[str] = None) -> NodeSet:
        """
        Minimizing the model from node with name `start` to node with name `end` base
        on self.settings. Find culprits that causes FxNetMinimizerRunFuncError or
        FxNetMinimizerResultMismatchError errors.

        Args:
            start: The name of the node where we want to start minimizing. If set
                to None, then we'll start with the first node of the model.
            end: The name of the node where we want to terminate minimizing. If
                set to None, we'll end with the last node of the model.

        Returns:
            nodes: A list of nodes that causes FxNetMinimizerRunFuncError or
                FxNetMinimizerResultMismatchError errors during minimizing.
        """

        print(self.settings)
        print(self.module.graph)
        nodes = self._collect_nodes(start, end)

        if self.settings.traverse_method == "sequential":
            return self._sequential_traverse(nodes)

        if self.settings.traverse_method == "binary":
            return self._binary_traverse(nodes)

        if self.settings.traverse_method == "accumulate":
            return self._accumulate_traverse(nodes)

        raise RuntimeError(f"Unknow traverse method {self.settings.traverse_method}!")
