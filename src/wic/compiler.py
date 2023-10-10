import argparse
import copy
import json
import os
from pathlib import Path
from typing import Dict, List

import graphviz
from mergedeep import merge, Strategy
import networkx as nx
import yaml

from . import inference, utils, utils_cwl, utils_graphs, python_cwl_adapter
from .wic_types import (CompilerInfo, EnvData, ExplicitEdgeCalls,
                        ExplicitEdgeDefs, GraphData, GraphReps, Namespaces,
                        NodeData, RoseTree, Tool, Tools, WorkflowInputsFile,
                        Yaml, YamlTree, StepId)

# NOTE: This must be initialized in main.py and/or cwl_watcher.py
inference_rules: Dict[str, str] = {}


def compile_workflow(yaml_tree_ast: YamlTree,
                     args: argparse.Namespace,
                     namespaces: Namespaces,
                     subgraphs_: List[GraphReps],
                     explicit_edge_defs: ExplicitEdgeDefs,
                     explicit_edge_calls: ExplicitEdgeCalls,
                     input_mapping: Dict[str, List[str]],
                     output_mapping: Dict[str, str],
                     tools: Tools,
                     is_root: bool,
                     relative_run_path: bool,
                     testing: bool) -> CompilerInfo:
    """fixed-point wrapper around compile_workflow_once\n
    See https://en.wikipedia.org/wiki/Fixed_point_(mathematics)

    Args:
        yaml_tree_ast (YamlTree): A tuple of name and yml AST
        args (Any): all of the other positional arguments for compile_workflow_once
        kwargs (Any): all of the other keyword arguments for compile_workflow_once

    Returns:
        CompilerInfo: Contains the data associated with compiled subworkflows\n
        (in the Rose Tree) together with mutable cumulative environment\n
        information which needs to be passed through the recursion.
    """
    ast_modified = True
    yaml_tree = yaml_tree_ast
    # There ought to be at most one file format conversion between each step.
    # If everything is working correctly, we should thus reach the fixed point
    # in at most n-1 iterations. However, due to the possibility of bugs in the
    # implementation and/or spurious inputs, we should guarantee termination.
    max_iters = 100  # 100 ought to be plenty. TODO: calculate n-1 from steps:
    i = 0
    while ast_modified and i < max_iters:
        subgraphs = copy.deepcopy(subgraphs_)  # See comment below!
        compiler_info = compile_workflow_once(yaml_tree, args, namespaces, subgraphs,
                                              explicit_edge_defs, explicit_edge_calls,
                                              input_mapping, output_mapping,
                                              tools, is_root, relative_run_path, testing)
        node_data: NodeData = compiler_info.rose.data
        ast_modified = not yaml_tree.yml == node_data.yml
        if ast_modified:
            # import yaml
            # print(yaml.dump(node_data.yml))
            # print()
            yaml_tree = YamlTree(yaml_tree_ast.step_id, node_data.yml)
        i += 1

    # Overwrite subgraphs_ element-wise
    # This is a terrible hack due to the fact that the graphviz library API
    # only allows appending to the body. This introduces mutable state, so each
    # time we speculatively compile we accumulate duplicate nodes and edges.
    # The 'correct' solution is to store the nodes and edges that you want to
    # add in a separate data structure, return them from compile_workflow_once,
    # and only once we have reached the fixed point then add them here. Due to
    # labeling and styling and other graphviz metadata that is not trivial, so
    # instead we simply deepcopy and overwrite the bodies here.
    # (NOTE: We now have a separate GraphData structure, so the graphviz and
    # networkx representations can probably be removed from the recursion.)
    # (Also note that you have to do this element-wise; you cannot simply write
    # subgraphs_ = subgraphs because that will only overwrite the local binding
    # and thus it will not affect the call site of compile_workflow!)
    # TODO: overwrite the networkx subgraphs. For now this is okay because
    # we are only using the networkx graphs to do an isomorphism check in the
    # regression tests, in which case identical duplication will not matter.
    for i, subgraph_ in enumerate(subgraphs_):
        subgraph_.graphviz.body = subgraphs[i].graphviz.body
        subgraph_.graphdata.name = subgraphs[i].graphdata.name
        subgraph_.graphdata.nodes = subgraphs[i].graphdata.nodes
        subgraph_.graphdata.edges = subgraphs[i].graphdata.edges
        subgraph_.graphdata.subgraphs = subgraphs[i].graphdata.subgraphs

    if i == max_iters:
        print(yaml.dump(node_data.yml))
        raise Exception(f'Error! Maximum number of iterations ({max_iters}) reached in compile_workflow!')
    return compiler_info


def compile_workflow_once(yaml_tree_ast: YamlTree,
                          args: argparse.Namespace,
                          namespaces: Namespaces,
                          subgraphs: List[GraphReps],
                          explicit_edge_defs: ExplicitEdgeDefs,
                          explicit_edge_calls: ExplicitEdgeCalls,
                          input_mapping: Dict[str, List[str]],
                          output_mapping: Dict[str, str],
                          tools: Tools,
                          is_root: bool,
                          relative_run_path: bool,
                          testing: bool) -> CompilerInfo:
    """STOP: Have you read the Developer's Guide?? docs/devguide.md\n
    Recursively compiles yml workflow definition ASTs to CWL file contents

    Args:
        yaml_tree_ast (YamlTree): A tuple of name and yml AST
        args (argparse.Namespace): The command line arguments
        namespaces (Namespaces): Specifies the path in the yml AST to the current subworkflow
        subgraphs (List[Graph]): The graphs associated with the parent workflows of the current subworkflow
        explicit_edge_defs (ExplicitEdgeDefs): Stores the (path, value) of the explicit edge definition sites
        explicit_edge_calls (ExplicitEdgeCalls): Stores the (path, value) of the explicit edge call sites
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl().\n
        yml files that have been compiled to CWL SubWorkflows are also added during compilation.
        is_root (bool): True if this is the root workflow
        relative_run_path (bool): Controls whether to use subdirectories or\n
        just one directory when writing the compiled CWL files to disk
        testing: Used to disable some optional features which are unnecessary for testing.

    Raises:
        Exception: If any errors occur

    Returns:
        CompilerInfo: Contains the data associated with compiled subworkflows\n
        (in the Rose Tree) together with mutable cumulative environment\n
        information which needs to be passed through the recursion.
    """
    # NOTE: Use deepcopy so that when we delete wic: we don't modify any call sites
    (step_id, yaml_tree) = copy.deepcopy(yaml_tree_ast)
    yaml_path = step_id.stem
    # We also want another copy of the original AST so that if we need to modify it,
    # we can return the modified AST to the call site and re-compile.
    (yaml_path_orig, yaml_tree_orig) = copy.deepcopy(yaml_tree_ast)

    if not testing:
        print(' starting', ('  ' * len(namespaces)) + yaml_path)

    # Check for top-level yml dsl args
    wic = {'wic': yaml_tree.get('wic', {})}
    # import yaml; print(yaml.dump(wic))
    if 'wic' in yaml_tree:
        del yaml_tree['wic']
    wic_steps = wic['wic'].get('steps', {})

    yaml_stem = Path(yaml_path).stem

    (back_name_, yaml_tree) = utils.extract_backend(yaml_tree, wic['wic'], Path(yaml_path))
    steps: List[Yaml] = yaml_tree['steps']

    steps_keys = utils.get_steps_keys(steps)

    tools_stems = [stepid.stem for stepid in tools]
    subkeys = utils.get_subkeys(steps_keys, tools_stems)

    # Add headers
    # Use 1.0 because cromwell only supports 1.0 and we are not using 1.1 / 1.2 features.
    # Eventually we will want to use 1.2 to support conditional workflows and 1.3 to support loops.
    yaml_tree['cwlVersion'] = 'v1.2'
    yaml_tree['class'] = 'Workflow'
    yaml_tree['$namespaces'] = {'edam': 'https://edamontology.org/'}
    yaml_tree['$schemas'] = ['https://raw.githubusercontent.com/edamontology/edamontology/master/EDAM_dev.owl']

    # Collect workflow input parameters
    inputs_workflow = {}
    inputs_file_workflow = {}

    # Collect workflow input/output to workflow step input/output mappings
    input_mapping_copy = copy.deepcopy(input_mapping)
    output_mapping_copy = copy.deepcopy(output_mapping)

    # Collect the internal workflow output variables
    outputs_workflow = []
    vars_workflow_output_internal = []

    # Copy recursive explicit edge variable definitions and call sites.
    explicit_edge_defs_copy = copy.deepcopy(explicit_edge_defs)
    explicit_edge_calls_copy = copy.deepcopy(explicit_edge_calls)
    # Unlike the first copies which are mutably updated, these are returned
    # unmodified so that we can test that compilation is embedding independent.
    explicit_edge_defs_copy2 = copy.deepcopy(explicit_edge_defs)
    explicit_edge_calls_copy2 = copy.deepcopy(explicit_edge_calls)
    # Yet another copy for checkpointing
    explicit_edge_defs_chk = {}
    explicit_edge_calls_chk = {}

    # Collect recursive subworkflow data
    step_1_names = []
    sibling_subgraphs = []

    rose_tree_list = []

    graph = subgraphs[-1]  # Get the current graph
    graph_gv = graph.graphviz
    graph_nx = graph.networkx
    graphdata = graph.graphdata

    # plugin_ns = wic['wic'].get('namespace', 'global')

    tools_lst: List[Tool] = []

    for i, step_key in enumerate(steps_keys):
        step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        stem = Path(step_key).stem
        wic_step_i = wic_steps.get(f'({i+1}, {step_key})', {})
        # NOTE: See comments in read_ast_from_disk()
        plugin_ns_i = wic_step_i.get('wic', {}).get('namespace', 'global')  # plugin_ns ??
        stepid = StepId(stem, plugin_ns_i)

        # Recursively compile subworkflows, adding compiled cwl file contents to tools
        ast_modified = False
        if step_key in subkeys:
            # Extract the sub yaml file that we pre-loaded from disk.
            sub_yml = steps[i][step_key]['subtree']
            sub_yaml_tree = YamlTree(StepId(step_key, plugin_ns_i), sub_yml)

            # get the label (if any) from the subworkflow
            step_i_wic_graphviz = sub_yml.get('wic', {}).get('graphviz', {})
            label = step_i_wic_graphviz.get('label', step_key)
            style = step_i_wic_graphviz.get('style', '')

            subgraph_gv = graphviz.Digraph(name=f'cluster_{step_key}')
            subgraph_gv.attr(label=label)  # str(path)
            subgraph_gv.attr(color='lightblue')  # color of outline
            if style != '':
                subgraph_gv.attr(style=style)
            subgraph_nx = nx.DiGraph()
            graphdata = GraphData(step_key)
            subgraph = GraphReps(subgraph_gv, subgraph_nx, graphdata)

            # Checkpoint / restore environment
            if wic_step_i.get('wic', {}).get('environment', {}).get('action', '') == 'checkpoint':
                # print('checkpointing environment')
                explicit_edge_defs_chk = copy.deepcopy(explicit_edge_defs_copy)
                explicit_edge_calls_chk = copy.deepcopy(explicit_edge_calls_copy)
            if wic_step_i.get('wic', {}).get('environment', {}).get('action', '') == 'restore':
                save_defs = wic_step_i.get('wic', {}).get('environment', {}).get('save_defs', [])
                merge_keyvals_defs = {}
                # merge_keyvals_calls = {}
                for key in save_defs:
                    merge_keyvals_defs[key] = explicit_edge_defs_copy[key]
                    # merge_keyvals_calls[key] = explicit_edge_calls_copy[key]
                # print('restoring environment')
                explicit_edge_defs_copy = copy.deepcopy(explicit_edge_defs_chk)  # deepcopy?
                explicit_edge_calls_copy = copy.deepcopy(explicit_edge_calls_chk)  # deepcopy?
                explicit_edge_defs_copy.update(merge_keyvals_defs)
                # explicit_edge_calls_copy.update(merge_keyvals_calls)

            sub_compiler_info = compile_workflow(sub_yaml_tree, args, namespaces + [step_name_i],
                                                 subgraphs + [subgraph], explicit_edge_defs_copy,
                                                 explicit_edge_calls_copy,
                                                 input_mapping_copy, output_mapping_copy,
                                                 tools, False, relative_run_path, testing)

            sub_rose_tree = sub_compiler_info.rose
            rose_tree_list.append(sub_rose_tree)

            sub_node_data: NodeData = sub_rose_tree.data
            sub_env_data = sub_compiler_info.env

            ast_modified = not sub_yaml_tree.yml == sub_node_data.yml
            if ast_modified:
                # Propagate the updated yaml_tree (and wic: tags) upwards.
                # Since we already called ast.merge_yml_trees() before initially
                # compiling, the only way the child wic: tags can differ from
                # the parent is if there were modifications during compilation.
                # In other words, it should be safe to simply replace the
                # relevant yaml_tree and wic: tags in the parent with the child
                # values.
                print('AST modified', step_key)
                wic_steps[f'({i+1}, {step_key})'] = {'wic': sub_node_data.yml.get('wic', {})}
                wic_step_i = wic_steps.get(f'({i+1}, {step_key})', {})
                # import yaml
                # print(yaml.dump(wic_steps))

            # Add arguments to the compiled subworkflow (if any), being careful
            # to remove any child wic: metadata annotations. Post-compilation
            # arguments can now be added either directly inline or as metadata.
            wic_step_i_copy = copy.deepcopy(wic_step_i)
            if 'wic' in wic_step_i_copy:
                del wic_step_i_copy['wic']
            # NOTE: To support overloading, the metadata args must overwrite the parent args!
            args_provided_dict = merge(steps[i][step_key]['parentargs'], wic_step_i_copy,
                                       strategy=Strategy.TYPESAFE_REPLACE)  # TYPESAFE_ADDITIVE ?
            steps[i][step_key] = args_provided_dict

            sibling_subgraphs.append(sub_node_data.graph)  # TODO: Just subgraph?
            step_1_names.append(sub_node_data.step_name_1)
            tool_i = Tool(stem + '.cwl', sub_node_data.compiled_cwl)

            # Initialize the above from recursive values.
            # Do not initialize inputs_workflow. See comment below.
            # inputs_workflow.update(sub_node_data.inputs_workflow)
            inputs_namespaced_list = []
            for k, val in sub_env_data.inputs_file_workflow.items():
                input_namespaced = (f'{step_name_i}___{k}', val)  # _{step_key}_input___{k}
                inputs_namespaced_list.append(input_namespaced)
            inputs_namespaced = dict(inputs_namespaced_list)
            inputs_file_workflow.update(inputs_namespaced)

            input_mapping_copy_namespaced = dict([(f'{step_name_i}___{k}', val) for k, val in
                                                  sub_env_data.input_mapping.items() if k not in input_mapping])
            input_mapping_copy.update(input_mapping_copy_namespaced)

            output_mapping_copy_namespaced = dict([(f'{step_name_i}___{k}', val) for k, val in
                                                   sub_env_data.output_mapping.items() if k not in output_mapping])
            output_mapping_copy.update(output_mapping_copy_namespaced)

            vars_workflow_output_internal += sub_env_data.vars_workflow_output_internal
            explicit_edge_defs_copy.update(sub_env_data.explicit_edge_defs)
            explicit_edge_calls_copy.update(sub_env_data.explicit_edge_calls)
        else:
            if 'python_script' == step_key:
                # This generates a CWL CommandLineTool for an arbitrary python script just-in-time,
                # i.e. immediately before compilation of the python_script yml tag below.
                yml_args = copy.deepcopy(steps[i][step_key]['in'])
                python_script_path = yml_args.get('script', '')
                # NOTE: The existence of the script: tag should now be guaranteed in the schema
                del yml_args['script']
                python_script_docker_pull = yml_args.get('dockerPull', '')  # Optional
                if 'dockerPull' in yml_args:
                    del yml_args['dockerPull']
                    del steps[i][step_key]['in']['dockerPull']
                root_yml_dir_abs = Path(args.yaml).parent.absolute()
                python_script_path = root_yml_dir_abs / Path(python_script_path)
                python_script_mod = Path(python_script_path).name[:-3]
                module = python_cwl_adapter.get_module(python_script_mod, python_script_path, yml_args)
                generated_cwl = python_cwl_adapter.generate_CWL_CommandLineTool(
                    module.inputs, module.outputs, python_script_docker_pull)
                filepath = 'autogenerated/' + '___'.join(namespaces + [python_script_mod + '.cwl'])
                with open(filepath, mode='w', encoding='utf-8') as f:
                    f.write(yaml.dump(generated_cwl, sort_keys=False, line_break='\n', indent=2))
                # step_id = StepId(python_script_mod, 'global')
                # NOTE: The name 'python_script' is obviously not namespaced, and may
                # prevent using more than one script in a workflow. However, filepath
                # is indeed namespaced, so maybe it is okay.
                step_id_ = StepId('python_script', 'global')
                tool_i = Tool(filepath, generated_cwl)
                tools[step_id_] = tool_i
            else:
                tool_i = tools[stepid]
        tools_lst.append(tool_i)

        if not testing:
            # Disable for testing because when testing in parallel, the *.gv Graphviz files
            # can be written/read to/from disk simultaneously, which results in
            # intermittent 'syntax errors'.
            pass
            # Actually, this is a significant performance bottleneck and isn't really necessary.
            # utils_graphs.make_tool_dag(stem, tool_i, args.graph_dark_theme)

        # Add run tag, using relative or flat-directory paths
        # NOTE: run: path issues were causing test_cwl_embedding_independence()
        # to fail, so I simply ignore the run tag in that test.
        run_path = tool_i.run_path
        # NOTE: run_path is always relative; relative_run_path should probably
        # be called use_subdirs, because it simply determines if subworkflows
        # should be written to subdirectories or if everything should be
        # written to autogenerated/
        if relative_run_path:
            if step_key in subkeys:
                run_path = step_name_i + '/' + run_path
            else:
                run_path = os.path.relpath(run_path, 'autogenerated/')
                run_path = ('../' * len(namespaces)) + run_path
        else:
            if step_key in subkeys:
                run_path = '___'.join(namespaces + [step_name_i, run_path])
            else:
                run_path = os.path.relpath(run_path, 'autogenerated/')

        if steps[i][step_key]:
            if not 'run' in steps[i][step_key]:
                steps[i][step_key].update({'run': run_path})
        else:
            steps[i] = {step_key: {'run': run_path}}

        # Generate intermediate file names between steps.
        if 'in' not in steps[i][step_key]:
            steps[i][step_key]['in'] = {}

        if 'cwl_watcher' == step_key:
            in_dict_in = steps[i][step_key]['in']  # NOTE: Mutates in_dict_in
            utils.write_absolute_yaml_tags(args, in_dict_in, namespaces, step_name_i, explicit_edge_calls_copy)

        args_provided = []
        if steps[i][step_key] and 'in' in steps[i][step_key]:
            args_provided = list(steps[i][step_key]['in'])
        # print(args_provided)

        in_tool = tool_i.cwl['inputs']
        # print(list(in_tool.keys()))
        if tool_i.cwl['class'] == 'CommandLineTool':
            args_required = [arg for arg in in_tool if not (in_tool[arg].get('default') or
                                                            # Check for optional arguments using both the '?' syntactic sugar, as well as the
                                                            # canonical null representation. See canonicalize_type in cwl_utils.py
                                                            (isinstance(in_tool[arg]['type'], str) and in_tool[arg]['type'][-1] == '?') or
                                                            (isinstance(in_tool[arg]['type'], List) and 'null' in in_tool[arg]['type']))]
        elif tool_i.cwl['class'] == 'Workflow':
            args_required = list(in_tool)

            if 'in' not in steps[i][step_key]:
                steps[i][step_key]['in'] = {key: key for key in args_required}
            else:
                # Add keys, but do not overwrite existing vals.
                for key in args_required:
                    if key not in steps[i][step_key]['in']:
                        steps[i][step_key]['in'][key] = key
        else:
            raise Exception('Unknown class', tool_i.cwl['class'])

        # Note: Some biobb config tags are not required in the cwl files, but are in
        # fact required in the python source code! See check_mandatory_property
        # (Solution: refactor all required arguments out of config and list
        # them as explicit inputs in the cwl files, then modify the python
        # files accordingly.)
        # print(args_required)

        sub_args_provided = [arg for arg in args_required if arg in explicit_edge_calls_copy]
        # print(sub_args_provided)

        label = step_key
        if args.graph_label_stepname:
            label = step_name_i
        step_node_name = '___'.join(namespaces + [step_name_i])

        if not tool_i.cwl['class'] == 'Workflow':
            wic_graphviz_step_i = wic_step_i.get('wic', {}).get('graphviz', {})
            label = wic_graphviz_step_i.get('label', label)
            default_style = 'rounded, filled'
            style = wic_graphviz_step_i.get('style', '')
            style = default_style if style == '' else default_style + ', ' + style
            attrs = {'label': label, 'shape': 'box', 'style': style, 'fillcolor': 'lightblue'}
            graph_gv.node(step_node_name, **attrs)
            graph_nx.add_node(step_node_name)
            graphdata.nodes.append((step_node_name, attrs))
        elif not (step_key in subkeys and len(namespaces) < args.graph_inline_depth):
            nssnode = namespaces + [step_name_i]
            # Just like in add_graph_edge(), here we can hide all of the details
            # below a given depth by simply truncating the node's namespaces.
            nssnode = nssnode[:(1 + args.graph_inline_depth)]
            step_node_name = '___'.join(nssnode)
            # NOTE: NOT wic_graphviz_step_i
            # get the label (if any) from the subworkflow
            # TODO: This causes test_cwl_embedding_independence to fail.
            # yml = sub_node_data.yml if ast_modified else sub_yaml_tree.yml
            # step_i_wic_graphviz = yml.get('wic', {}).get('graphviz', {})
            # TODO: For file format conversions, figure out why this is using
            # the label from the parent workflow.
            # label = step_i_wic_graphviz.get('label', label)
            default_style = 'rounded, filled'
            style = ''  # step_i_wic_graphviz.get('style', '')
            style = default_style if style == '' else default_style + ', ' + style
            attrs = {'label': label, 'shape': 'box', 'style': style, 'fillcolor': 'lightblue'}
            graph_gv.node(step_node_name, **attrs)
            graph_nx.add_node(step_node_name)
            graphdata.nodes.append((step_node_name, attrs))

        # NOTE: sub_args_provided are handled within the args_required loop below
        for arg_key in args_provided:
            # Extract input value into separate yml file
            # Replace it here with a new variable name
            arg_val = steps[i][step_key]['in'][arg_key]
            # Convert native YAML to a JSON-encoded string for specific tags.
            tags = ['config']
            if arg_key in tags and isinstance(arg_val, Dict):
                arg_val = json.dumps(arg_val)  # Do NOT wrap config: in {'source': ...}
            elif isinstance(arg_val, str):
                arg_val = {'source': arg_val}
            # Use triple underscore for namespacing so we can split later
            in_name = f'{step_name_i}___{arg_key}'  # {step_name_i}_input___{arg_key}

            # Add auxiliary inputs for scatter steps
            if str(arg_key).startswith('__') and str(arg_key).endswith('__'):
                in_dict = {'type': arg_val['type']}
                inputs_workflow.update({in_name: in_dict})
                in_dict = {**in_dict, 'value': arg_val}
                inputs_file_workflow.update({in_name: in_dict})
                steps[i][step_key]['in'][arg_key] = {'source': in_name}
                continue
            # check if the argument name is correct
            if arg_key in in_tool:
                in_dict = utils_cwl.copy_cwl_input_output_dict(in_tool[arg_key], True)
            else:
                raise Exception(f'Error! The "{step_key}" step in "{yaml_stem}.yml" '
                                f'does not have an argument named "{arg_key}".')

            if isinstance(arg_val, Dict) and arg_val['source'][0] == '~':
                # NOTE: This is somewhat of a hack; it is useful for when
                # inference fails and when you cannot make an explicit edge.
                arg_val['source'] = arg_val['source'][1:]  # Remove ~

                # Subworkflows which use workflow inputs: variables ~var cannot
                # (yet) be inlined. Somehow, if they are not marked with
                # inlineable: False, test_inline_subworkflows can still pass.
                # This Exception will (correctly) cause such inlineing tests to fail.
                if arg_val['source'] not in yaml_tree.get('inputs', {}):
                    inputs = yaml_tree.get('inputs', {})
                    unbound_lit_var = 'Error! Unbound literal variable ~'
                    if inputs == {}:
                        raise Exception(f"{unbound_lit_var}{arg_val['source']} not in inputs: tag in {yaml_stem}.yml")
                    inputs_dump = yaml.dump({'inputs': inputs})
                    raise Exception(f"{unbound_lit_var}{arg_val['source']} not in\n{inputs_dump}\nin {yaml_stem}.yml")

                inputs_key_dict = yaml_tree['inputs'][arg_val['source']]
                if 'doc' in inputs_key_dict:
                    inputs_key_dict['doc'] += '\\n' + in_dict.get('doc', '')
                else:
                    inputs_key_dict['doc'] = in_dict.get('doc', '')
                if 'label' in inputs_key_dict:
                    inputs_key_dict['label'] += '\\n' + in_dict.get('label', '')
                else:
                    inputs_key_dict['label'] = in_dict.get('label', '')

                if arg_val['source'] in input_mapping_copy:
                    input_mapping_copy[arg_val['source']].append(in_name)
                else:
                    input_mapping_copy[arg_val['source']] = [in_name]
                # TODO: We have ~ syntax for input mapping; no notation for output mapping!
                # For now, use nasty hack below.

                steps[i][step_key]['in'][arg_key] = arg_val  # Leave un-evaluated? or
                """# Prepend the autogenerated step name
                (step_name, out_name) = arg_val['source'].split('/')
                for j in reversed(range(i)):
                    if step_name == steps_keys[j]:
                        tool_j = tools_lst[j]
                        out_keys = list(tool_j.cwl['outputs'])
                        if out_name not in out_keys:
                            print(f'Error! {out_name} not in {out_keys}')
                        step_name_j = utils.step_name_str(yaml_stem, j, steps_keys[j])
                        arg_val['source'] = step_name_j + '/' + out_name
                        break
                steps[i][step_key]['in'][arg_key] = arg_val"""
            elif isinstance(arg_val, Dict) and arg_val['source'][0] == '&':
                arg_val['source'] = arg_val['source'][1:]  # Remove &
                # print('arg_key, arg_val['source']', arg_key, arg_val['source'])
                # NOTE: There can only be one definition, but multiple call sites.
                if not explicit_edge_defs_copy.get(arg_val['source']):
                    # if first time encountering arg_val, i.e. if defining
                    inputs_workflow.update({in_name: in_dict})
                    in_dict = {**in_dict, 'value': arg_val}
                    inputs_file_workflow.update({in_name: in_dict})
                    steps[i][step_key]['in'][arg_key] = {'source': in_name}
                    explicit_edge_defs_copy.update({arg_val['source']: (namespaces + [step_name_i], arg_key)})
                    # Add a 'dummy' value to explicit_edge_calls, because
                    # that determines sub_args_provided when the recursion returns.
                    explicit_edge_calls_copy.update({in_name: (namespaces + [step_name_i], arg_key)})
                    # TODO: Show input node?
                else:
                    raise Exception(f"Error! Multiple definitions of &{arg_val['source']}!")
            elif isinstance(arg_val, Dict) and arg_val['source'][0] == '*' and 'cwl_watcher' not in step_key:
                # NOTE: Exclude cwl_watcher from explicit edge dereferences.
                # Since cwl_watcher requires explicit filenames for globbing,
                # we do not want to replace them with internal CWL dependencies!
                arg_val['source'] = arg_val['source'][1:]  # Remove *
                if not explicit_edge_defs_copy.get(arg_val['source']):
                    if is_root and not testing:
                        # Even if is_root, we don't want to raise an Exception
                        # here because in test_cwl_embedding_independence, we
                        # recompile all subworkflows as if they were root. That
                        # will cause this code path to be taken but it is not
                        # actually an error. Add a CWL input for testing only.
                        raise Exception(f"Error! No definition found for &{arg_val['source']}!")
                    inputs_workflow.update({in_name: in_dict})
                    steps[i][step_key]['in'][arg_key] = {'source': in_name}
                    # Add a 'dummy' value to explicit_edge_calls anyway, because
                    # that determines sub_args_provided when the recursion returns.
                    explicit_edge_calls_copy.update({in_name: (namespaces + [step_name_i], arg_key)})
                else:
                    (nss_def_init, var) = explicit_edge_defs_copy[arg_val['source']]

                    nss_def_embedded = var.split('___')[:-1]
                    nss_call_embedded = arg_key.split('___')[:-1]
                    nss_def = nss_def_init + nss_def_embedded
                    # [step_name_i] is correct; nss_def_init already contains step_name_j from the recursive call
                    nss_call = namespaces + [step_name_i] + nss_call_embedded

                    nss_def_inits, nss_def_tails = utils.partition_by_lowest_common_ancestor(nss_def, nss_call)
                    nss_call_inits, nss_call_tails = utils.partition_by_lowest_common_ancestor(nss_call, nss_def)
                    # nss_def and nss_call are paths into the abstract syntax tree 'call stack'.
                    # This defines the 'common namespace' in the call stack w.r.t. the inits.
                    assert nss_def_inits == nss_call_inits

                    # TODO: Check this comment.
                    # Relative to the common namespace, if the call site of an explicit
                    # edge is at a depth > 1, (i.e. if it is NOT simply of the form
                    # last_namespace/input_variable) then we
                    # need to create inputs in all of the intervening CWL files
                    # so we can pass in the values from the outer scope(s). Here,
                    # we simply need to use in_name and add to inputs_workflow
                    # and explicit_edge_calls. The outer scope(s) are handled by
                    # the sub_args_provided clause below.
                    # Note that the outputs from the definition site are bubbled
                    # up the call stack until they reach the common namespace.
                    if (len(nss_call_tails) == 1 or
                            'valueFrom' in arg_val):  # TODO: This is a temporary hack to implement scattering.
                        # 'scatter' in wic_step_i): # TODO: Figure out why this doesn't work.
                        # TODO: Check this comment.
                        # The definition site recursion (only, if any) has completed
                        # and we are already in the common namespace, thus
                        # we need to pass in the value from the definition site.
                        # Note that since len(nss_call_tails) == 1,
                        # there will not be any call site recursion in this case.
                        var_slash = nss_def_tails[0] + '/' + '___'.join(nss_def_tails[1:] + [var])
                        arg_val['source'] = var_slash
                        steps[i][step_key]['in'][arg_key] = arg_val
                    elif len(nss_call_tails) > 1:
                        inputs_workflow.update({in_name: in_dict})
                        # Store explicit edge call site info up through the recursion.
                        d = {in_name: explicit_edge_defs_copy[arg_val['source']]}
                        # d = {in_name, (namespaces + [step_name_i], var)} # ???
                        explicit_edge_calls_copy.update(d)
                        arg_val['source'] = in_name
                        steps[i][step_key]['in'][arg_key] = arg_val
                    else:
                        # Since nss_call includes step_name_i, this should never happen...
                        raise Exception("Error! len(nss_call_tails) == 0! Please file a bug report!\n" +
                                        f'nss_def {nss_def}\n nss_call {nss_call}')

                    arg_keys = [in_name] if in_name in input_mapping_copy else [arg_key]
                    arg_keys = utils.get_input_mappings(input_mapping_copy, arg_keys,
                                                        (arg_key in yaml_tree.get('inputs', {})))

                    out_key_init = '___'.join(nss_def_init + [var])
                    out_key = utils.get_output_mapping(output_mapping_copy, out_key_init)

                    nss_def_embedded = out_key.split('___')[:-1]

                    # NOTE: This if statement is unmotivated and probably masking some other bug, but it works.
                    if out_key.startswith('___'.join(nss_def_init)):
                        nss_def = nss_def_embedded

                    # Add an edge, but in a carefully chosen subgraph.
                    # If you add an edge whose head/tail is outside of the subgraph,
                    # graphviz may segfault! Moreover, even if graphviz doesn't
                    # segfault, adding an edge in a given subgraph can cause the
                    # nodes themselves to be rendered in that subgraph, even
                    # though the nodes are defined in a different subgraph!
                    # The correct thing to do is to use the graph associated with
                    # the lowest_common_ancestor of the definition and call site.
                    # (This is the only reason we need to pass in all subgraphs.)
                    label = var.split('___')[-1]
                    graph_init = subgraphs[len(nss_def_inits)]
                    # Let's use regular blue for explicit edges.
                    # Use constraint=false ?
                    for arg_key_ in arg_keys:
                        # TODO: Double check that we can use the same graph_init for all edges.
                        # Since input_mapping_copy really just factors edges through a single workflow input,
                        # this should hopefully be correct.

                        # NOTE: This if statement is unmotivated and probably masking some other bug, but it works.
                        nss_call_embedded = arg_key_.split('___')[:-1]
                        if arg_key_.startswith('___'.join(namespaces + [step_name_i])):
                            nss_call = nss_call_embedded
                        elif arg_key_.startswith(step_name_i):
                            nss_call = namespaces + nss_call_embedded
                        else:
                            nss_call = namespaces + [step_name_i] + nss_call_embedded

                        utils_graphs.add_graph_edge(args, graph_init, nss_def, nss_call, label, color='blue')
            else:
                # NOTE: See comment above about excluding cwl_watcher from
                # explicit edge dereferences.
                if (isinstance(arg_val, Dict) and arg_val['source'][0] == '*' and
                        'cwl_watcher' in step_key and 'file_pattern' not in arg_key):
                    arg_val['source'] = arg_val['source'][1:]  # Remove *, except for file_pattern

                if (arg_key in steps[i][step_key].get('scatter', []) or
                        (isinstance(arg_val, Dict) and 'valueFrom' in arg_val)):
                    # Promote scattered input types to arrays
                    in_dict['type'] = {'type': 'array', 'items': in_dict['type']}

                inputs_workflow.update({in_name: in_dict})
                in_dict = {**in_dict, 'value': arg_val}
                inputs_file_workflow.update({in_name: in_dict})
                new_val = {'source': in_name}
                if isinstance(arg_val, Dict):
                    new_val = {**arg_val, **new_val}
                steps[i][step_key]['in'][arg_key] = new_val

                if args.graph_show_inputs:
                    input_node_name = '___'.join(namespaces + [step_name_i, arg_key])
                    attrs = {'label': arg_key, 'shape': 'box', 'style': 'rounded, filled', 'fillcolor': 'lightgreen'}
                    graph_gv.node(input_node_name, **attrs)
                    font_edge_color = 'black' if args.graph_dark_theme else 'white'
                    graph_gv.edge(input_node_name, step_node_name, color=font_edge_color)
                    graph_nx.add_node(input_node_name)
                    graph_nx.add_edge(input_node_name, step_node_name)
                    graphdata.nodes.append((input_node_name, attrs))
                    graphdata.edges.append((input_node_name, step_node_name, {}))

        for arg_key in args_required:
            # print('arg_key', arg_key)
            in_name = f'{step_name_i}___{arg_key}'
            if arg_key in args_provided:
                continue  # We already covered this case above.
            if in_name in inputs_file_workflow:
                # We provided an explicit argument (but not an edge) in a subworkflow,
                # and now we just need to pass it up to the root workflow.
                # print('passing', in_name)
                in_dict = utils_cwl.copy_cwl_input_output_dict(in_tool[arg_key])
                inputs_workflow.update({in_name: in_dict})
                arg_keyval = {arg_key: in_name}
                steps[i] = utils_cwl.add_yamldict_keyval_in(steps[i], step_key, arg_keyval)

                # Obviously since we supplied a value, we do NOT want to perform edge inference.
                continue
            if arg_key in sub_args_provided:  # Edges have been explicitly provided
                # The definition site recursion (if any) and the call site
                # recursion (yes, see above), have both completed and we are
                # now in the common namespace, thus
                # we need to pass in the value from the definition site.
                # Extract the stored defs namespaces from explicit_edge_calls.
                # (See massive comment above.)
                (nss_def_init, var) = explicit_edge_calls_copy[arg_key]

                nss_def_embedded = var.split('___')[:-1]
                nss_call_embedded = arg_key.split('___')[:-1]
                nss_def = nss_def_init + nss_def_embedded
                # [step_name_i] is correct; nss_def_init already contains step_name_j from the recursive call
                nss_call = namespaces + [step_name_i] + nss_call_embedded

                nss_def_inits, nss_def_tails = utils.partition_by_lowest_common_ancestor(nss_def, nss_call)
                nss_call_inits, nss_call_tails = utils.partition_by_lowest_common_ancestor(nss_call, nss_def)
                assert nss_def_inits == nss_call_inits

                nss_call_tails_stems = [utils.parse_step_name_str(x)[0] for x in nss_call_tails]
                arg_val = steps[i][step_key]['in'][arg_key]
                more_recursion = yaml_stem in nss_call_tails_stems and nss_call_tails_stems.index(yaml_stem) > 0
                if (nss_call_tails_stems == []) or more_recursion:
                    # i.e. (if 'dummy' value) or (if it is possible to do more recursion)
                    in_dict = utils_cwl.copy_cwl_input_output_dict(in_tool[arg_key])
                    inputs_workflow.update({in_name: in_dict})
                    steps[i][step_key]['in'][arg_key] = {'source': in_name}
                    # Store explicit edge call site info up through the recursion.
                    explicit_edge_calls_copy.update({in_name: explicit_edge_calls_copy[arg_key]})
                else:
                    # TODO: Check this comment.
                    # The definition site recursion (only, if any) has completed
                    # and we are already in the common namespace, thus
                    # we need to pass in the value from the definition site.
                    # Note that since len(nss_call_tails) == 1,
                    # there will not be any call site recursion in this case.
                    var_slash = nss_def_tails[0] + '/' + '___'.join(nss_def_tails[1:] + [var])
                    steps[i][step_key]['in'][arg_key] = {'source': var_slash}

                # NOTE: We already added an edge to the appropriate subgraph above.
                # TODO: vars_workflow_output_internal?
            else:
                if args.inference_disable:
                    continue
                conversions: List[StepId] = []
                in_name_in_inputs_file_workflow: bool = (in_name in inputs_file_workflow)
                arg_key_in_yaml_tree_inputs: bool = (arg_key in yaml_tree.get('inputs', {}))
                steps[i] = inference.perform_edge_inference(args, tools, tools_lst, steps_keys,
                                                            yaml_stem, i, steps, arg_key, graph, is_root, namespaces,
                                                            vars_workflow_output_internal, input_mapping_copy, output_mapping_copy, inputs_workflow, in_name,
                                                            in_name_in_inputs_file_workflow, arg_key_in_yaml_tree_inputs, conversions, wic_steps, testing)
                # NOTE: For now, perform_edge_inference mutably appends to
                # inputs_workflow and vars_workflow_output_internal.

                # Automatically insert file format conversion
                conversions = list(set(conversions))  # Remove duplicates
                if len(conversions) != 0 and args.insert_steps_automatically:
                    conversion = conversions[0]
                    print('Automaticaly inserting file format conversion', conversion, i)
                    if len(conversions) != 1:
                        print('Warning! More than one file format conversion! Choosing', conversion)

                    yaml_tree_mod = insert_step_into_workflow(yaml_tree_orig, conversion, tools, i)

                    node_data = NodeData(namespaces, yaml_stem, yaml_tree_mod, yaml_tree, {},
                                         explicit_edge_defs_copy2, explicit_edge_calls_copy2,
                                         graph, inputs_workflow, '')
                    rose_tree = RoseTree(node_data, rose_tree_list)
                    env_data = EnvData(input_mapping_copy, output_mapping_copy,
                                       inputs_file_workflow, vars_workflow_output_internal,
                                       explicit_edge_defs_copy, explicit_edge_calls_copy)
                    compiler_info = CompilerInfo(rose_tree, env_data)
                    # node_data_dummy = NodeData(None, None, yaml_tree_mod, None, None, None, None, None, None, None)
                    # compiler_info_dummy = CompilerInfo(RoseTree(node_data_dummy, None), None)
                    return compiler_info

        # Add CommandLineTool/Subworkflow outputs tags to workflow out tags.
        # Note: Add all output tags for now, but depending on config options,
        # not all output files will be generated. This may cause an error.
        out_keyvals = {}
        for out_key, out_dict in tool_i.cwl['outputs'].items():
            out_keyvals[out_key] = utils_cwl.copy_cwl_input_output_dict(out_dict)
            # print(out_key, out_keyvals[out_key])
        if not out_keyvals:  # FYI out_keyvals should never be {}
            print(f'Error! no outputs for step {step_key}')
        outputs_workflow.append(out_keyvals)

        steps[i] = utils_cwl.add_yamldict_keyval_out(steps[i], step_key, list(tool_i.cwl['outputs'].keys()))

        # print()

    # NOTE: add_subgraphs currently mutates graph
    wic_graphviz = wic['wic'].get('graphviz', {})
    ranksame_strs = wic_graphviz.get('ranksame', [])
    ranksame_pairs = [utils.parse_int_string_tuple(x) for x in ranksame_strs]
    steps_ranksame = []
    for num, name in ranksame_pairs:
        step_name_num = utils.step_name_str(yaml_stem, num-1, name)
        step_name_nss = '___'.join(namespaces + [step_name_num])
        steps_ranksame.append(f'"{step_name_nss}"')  # Escape with double quotes.
    utils_graphs.add_subgraphs(args, graph, sibling_subgraphs, namespaces, step_1_names, steps_ranksame)
    step_name_1 = utils.get_step_name_1(step_1_names, yaml_stem, namespaces, steps_keys, subkeys)

    # Add the provided workflow inputs to the workflow inputs from each step
    inputs_combined = {**yaml_tree.get('inputs', {}), **inputs_workflow}
    yaml_tree.update({'inputs': inputs_combined})

    # NOTE: This is a nasty hack because we don't have any syntax for mapping workflow outputs.
    for k, v in yaml_tree.get('outputs', {}).items():
        # Assume the user has manually added the correct namespaced CWL dependency.
        output_mapping_copy[k] = v['outputSource'].replace('/', '___')

    vars_workflow_output_internal = list(set(vars_workflow_output_internal))  # Get uniques
    # (Why are we getting uniques?)
    workflow_outputs = utils_cwl.get_workflow_outputs(args, namespaces, is_root, yaml_stem,
                                                      steps, outputs_workflow, vars_workflow_output_internal, graph, tools_lst, step_node_name)
    # Add the provided workflow outputs to the workflow outputs from each step
    outputs_combined = {**yaml_tree.get('outputs', {}), **workflow_outputs}
    yaml_tree.update({'outputs': outputs_combined})

    # NOTE: currently mutates yaml_tree (maybe)
    utils_cwl.maybe_add_requirements(yaml_tree, tools, steps_keys, wic_steps, subkeys)

    # Finally, rename the steps to be unique
    # and convert the list of steps into a dict
    steps_dict = {}
    for i, step_key in enumerate(steps_keys):
        step_name_i = utils.step_name_str(yaml_stem, i, step_key)
        # steps[i] = {step_name_i: steps[i][step_key]}
        steps_dict.update({step_name_i: steps[i][step_key]})
    yaml_tree.update({'steps': steps_dict})

    # Dump the workflow inputs to a separate yml file.
    yaml_inputs: WorkflowInputsFile = {}
    for key, in_dict in inputs_file_workflow.items():
        new_keyval: WorkflowInputsFile = {}
        if 'File' == in_dict['type']:
            # path = Path(in_dict['value']).name # NOTE: Use .name ?
            newval = {'class': 'File', 'path': in_dict['value']['source']}  # type: ignore
            if 'format' in in_dict:
                in_format = in_dict['format']
                if isinstance(in_format, List):
                    in_format = list(set(in_format))  # get uniques
                    if len(in_format) > 1:
                        print(f'NOTE: More than one input file format for {key}')
                        print(f'formats: {in_format}')
                        print(f'Choosing {in_format[0]}')
                    in_format = in_format[0]
                newval['format'] = in_format
            new_keyval = {key: newval}
        elif 'Directory' == in_dict['type']:
            dir = Path(in_dict['value']['source'])  # type: ignore
            if not dir.is_absolute():
                dir = Path('autogenerated') / dir
            dir.mkdir(parents=True, exist_ok=True)
            newval = {'class': 'Directory', 'location': in_dict['value']['source']}  # type: ignore
            new_keyval = {key: newval}
        # TODO: Check for all valid types?
        else:
            # We cannot store string values as a dict, so use type: ignore
            arg_val = in_dict['value']
            new_val = arg_val['source'] if isinstance(arg_val, Dict) else arg_val
            new_keyval = {key: new_val}
        # else:
        #    raise Exception(f"Error! Unknown type: {in_dict['type']}")
        yaml_inputs.update(new_keyval)

    if not testing:
        print('finishing', ('  ' * len(namespaces)) + yaml_path)
    # Note: We do not necessarily need to return inputs_workflow.
    # 'Internal' inputs are encoded in yaml_tree. See Comment above.
    node_data = NodeData(namespaces, yaml_stem, yaml_tree_orig, yaml_tree, yaml_inputs,
                         explicit_edge_defs_copy2, explicit_edge_calls_copy2,
                         graph, inputs_workflow, step_name_1)
    rose_tree = RoseTree(node_data, rose_tree_list)
    env_data = EnvData(input_mapping_copy, output_mapping_copy, inputs_file_workflow, vars_workflow_output_internal,
                       explicit_edge_defs_copy, explicit_edge_calls_copy)
    compiler_info = CompilerInfo(rose_tree, env_data)
    return compiler_info


def insert_step_into_workflow(yaml_tree_orig: Yaml, stepid: StepId, tools: Tools, i: int) -> Yaml:
    """Inserts the step with given stepid into a workflow at the given index.

    Args:
        yaml_tree_orig (Yaml): The original Yaml tree
        stepid (StepId): The name of the workflow step to be inserted.
        tools (Tools): The CWL CommandLineTool definitions found using get_tools_cwl().\n
        yml files that have been compiled to CWL SubWorkflows are also added during compilation.
        i (int): The index to insert the new workflow step

    Returns:
        Yaml: A modified Yaml tree with the given stepid inserted at index i
    """
    yaml_tree_mod = yaml_tree_orig
    steps_mod: List[Yaml] = yaml_tree_mod['steps']
    steps_mod.insert(i, {stepid.stem: None})

    # Add inference rules annotations (i.e. for file format conversion)
    tool = tools[stepid]
    out_tool = tool.cwl['outputs']

    inference_rules_dict = {}
    for out_key, out_val in out_tool.items():
        if 'format' in out_val:
            inference_rules_dict[out_key] = inference_rules.get(out_val['format'], 'default')
    inf_dict = {'wic': {'inference': inference_rules_dict}}
    keystr = f'({i+1}, {stepid.stem})'  # The yml file uses 1-based indexing

    if 'wic' in yaml_tree_mod:
        if 'steps' in yaml_tree_mod['wic']:
            yaml_tree_mod['wic']['steps'] = utils.reindex_wic_steps(yaml_tree_mod['wic']['steps'], i)
            yaml_tree_mod['wic']['steps'][keystr] = inf_dict
        else:
            yaml_tree_mod['wic'].update({'steps': {keystr: inf_dict}})
    else:
        yaml_tree_mod.update({'wic': {'steps': {keystr: inf_dict}}})
    return yaml_tree_mod
