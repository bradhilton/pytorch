# mypy: allow-untyped-defs
import argparse
import copy
import functools
import io
import logging
import os
import shutil
import subprocess
import sys
import textwrap
import uuid
from importlib import import_module
from tempfile import TemporaryFile
from typing import Any, Callable, Dict, Optional, Union

import torch
import torch.fx as fx
import torch.nn as nn
from torch._dynamo.debug_utils import (
    _cuda_system_info_comment,
    AccuracyError,
    backend_accuracy_fails,
    BuckTargetWriter,
    cast_to_fp64,
    extra_imports,
    generate_config_string,
    helper_for_dump_minify,
    InputReader,
    InputWriter,
    MAX_CONSTANT_NUMEL_INLINE,
    minifier_dir,
    NNModuleToString,
    NopInputReader,
    same_two_models,
)
from torch._dynamo.utils import clone_inputs, counters, same

from torch._functorch.aot_autograd import aot_export_module, get_aot_graph_name

from torch.export import export, ExportedProgram
from torch.fx.experimental.proxy_tensor import make_fx
from torch.fx.experimental.symbolic_shapes import (
    fx_placeholder_targets,
    has_free_symbols,
)
from torch.hub import tqdm

from .. import config

log = logging.getLogger(__name__)


inductor_config = import_module("torch._inductor.config")
use_buck = inductor_config.is_fbcode()

# TODO: consider combine with after_aot.py?


def dump_to_minify(
    exported_program: ExportedProgram,
    compiler_name: str,
    options: Optional[Dict[str, Any]] = None,
):
    out = io.StringIO()
    # TODO: factor this out
    subdir = os.path.join(minifier_dir(), "checkpoints")
    if not os.path.exists(subdir):
        os.makedirs(subdir, exist_ok=True)
    save_graph_repro(
        out,
        exported_program,
        compiler_name,
        save_dir=subdir,
        command="minify",
        options=options,
    )
    print(subdir)
    return helper_for_dump_minify(out.getvalue())


def save_graph_repro(
    fd,
    exported_program: ExportedProgram,
    compiler_name,
    *,
    options: Optional[Dict[str, str]] = None,
    stable_output=False,
    save_dir=None,
    command="run",
    accuracy=None,
    check_str=None,
):
    fd.write(
        generate_compiler_repro_string(
            exported_program,
            options=options,
            stable_output=stable_output,
            save_dir=save_dir,
        )
    )
    if accuracy is None:
        accuracy = "_accuracy" in compiler_name
    fd.write("if __name__ == '__main__':\n")
    fd.write("    from torch._dynamo.repro.aoti import run_repro\n")
    fd.write(
        f"    with torch.no_grad():\n"
        f"        run_repro(exported_program, config_patches=options, accuracy={accuracy!r}, command={command!r}, "
        f"save_dir={save_dir!r}, check_str={check_str!r})\n"
    )


def dump_compiler_graph_state(gm, args, compiler_name, *, accuracy=None):
    subdir = os.path.join(minifier_dir(), "checkpoints")
    if not os.path.exists(subdir):
        os.makedirs(subdir, exist_ok=True)
    file_name = os.path.join(subdir, f"{len(gm.graph.nodes)}.py")
    log.warning(
        "Writing checkpoint with %s nodes to %s", len(gm.graph.nodes), file_name
    )
    exported_program = torch.export.export(gm, tuple(args))
    with open(file_name, "w") as fd:
        save_graph_repro(
            fd, exported_program, compiler_name, save_dir=subdir, accuracy=accuracy
        )
    curdir = os.getcwd()
    repro_path = os.path.join(curdir, "repro.py")
    try:
        shutil.copyfile(file_name, repro_path)
        log.warning("Copying repro file for convenience to %s", repro_path)
        if use_buck:
            BuckTargetWriter(file_name).write()
    except OSError:
        log.warning("No write permissions for %s", repro_path)


# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #
#                           DUMP REPROS
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ #


def generate_compiler_repro_string(
    exported_program,
    *,
    options: Optional[Dict[str, str]] = None,
    stable_output=False,
    save_dir=None,
):
    model_str = textwrap.dedent(
        f"""
import torch
import torch._inductor.inductor_prims

{generate_config_string(stable_output=stable_output)}

isolate_fails_code_str = None

{extra_imports}

        """
    )
    if not stable_output:
        model_str += f"# torch version: {torch.version.__version__}\n"
        if hasattr(torch.version, "cuda"):
            model_str += f"# torch cuda version: {torch.version.cuda}\n"
        if hasattr(torch.version, "git_version"):
            model_str += f"# torch git version: {torch.version.git_version}\n\n\n"
        model_str += _cuda_system_info_comment()

    # model_str += NNModuleToString.convert(gm)

    ep_path = os.path.join(save_dir, "exported_program.pt2")
    torch.export.save(exported_program, ep_path)

    # writer = InputWriter(save_dir)
    # for placeholder, arg in zip(fx_placeholder_targets(gm), args):
    #     if isinstance(arg, (int, torch.SymInt)):
    #         writer.symint(placeholder, arg)
    #     elif isinstance(arg, torch.Tensor):
    #         # TODO: improve these names with FQN
    #         writer.tensor(placeholder, arg)
    #     elif arg is None:
    #         writer.const(placeholder)
    #     else:
    #         raise TypeError(f"arg is neither SymInt/int nor torch.Tensor, {arg}")

    # model_str += "\n".join(writer.lines()) + "\n"

    # model_str += "mod = Repro()\n"
    model_str += f"exported_program = torch.export.load('{ep_path}')\n"
    model_str += f"# print(exported_program.graph)\n"
    model_str += f"options={options}\n"
    return model_str


def repro_common(options, exported_program):

    # if not hasattr(load_args, "_version"):
    #     log.warning(
    #         "load_args does not have a _version attribute, please file a bug to PyTorch "
    #         "and describe how you generate this repro script"
    #     )
    # else:
    #     if load_args._version > 0:
    #         log.warning(
    #             "load_args is version %s, but this version of PyTorch only supports "
    #             "version 0.  We will try to run it anyway but there may be an incompatibility; "
    #             "if so, try upgrading your version of PyTorch.",
    #             load_args._version,
    #         )

    # nop_reader = NopInputReader()
    # load_args(nop_reader)

    # with tqdm(desc="Loading inputs", total=nop_reader.total) as pbar:
    #     input_reader = InputReader(save_dir=options.save_dir, pbar=pbar)
    #     load_args(input_reader)
    #     args = input_reader.args

    # # convert args from list to tuple to respect export input spec
    # args = tuple(args)
    # # Export the model to FX graph
    # mod = export(mod, args).module()

    # torch._inductor.config.generate_intermediate_hooks = True
    mod = exported_program.module()
    args, kwargs = exported_program.example_inputs
    return mod, args, kwargs


def repro_get_args(options, exported_program, config_patches):
    mod, args, kwargs = repro_common(options, exported_program)
    return mod, args, kwargs


def repro_run(options, exported_program, config_patches):
    from torch._inductor import _aoti_compile_and_package_inner, aoti_load_package

    mod, args, kwargs = repro_common(options, exported_program)

    # device = options.device
    # if device != "cpu" and not device.startswith("cuda"):
    #     raise RuntimeError("Unsupported device " + device)

    from torch.cuda import synchronize

    # so_path = compile_fx_aot(mod, args, config_patches=config_patches)
    # compiled = torch._export.aot_load(so_path, device=device)
    package_path = _aoti_compile_and_package_inner(
        mod,
        args,
        kwargs,
        load_and_run=False,
        inductor_configs=config_patches,
    )
    compiled = aoti_load_package(package_path)
    assert not isinstance(compiled, str)

    need_sync = False

    for arg in args:
        if isinstance(arg, torch.Tensor) and arg.is_cuda:
            need_sync = True
            break

    compiled(*args)

    if need_sync:
        synchronize()  # ensure segfaults are surfaced


def repro_minify(options, exported_program, config_patches):
    from functorch.compile import minifier
    from torch._inductor import _aoti_compile_and_package_inner

    mod, args, kwargs = repro_common(options, exported_program)
    compiler_name = "aot_inductor"

    # device = options.device
    # if device != "cpu" and not device.startswith("cuda"):
    #     raise RuntimeError("Unsupported device " + device)

    def module_fails(gm, flat_example_inputs, check_str=None):
        try:
            _aoti_compile_and_package_inner(
                gm,
                flat_example_inputs,
                kwargs,
                load_and_run=True,
                inductor_configs=config_patches,
            )
            return False
        except Exception as e:
            if check_str is not None and check_str not in repr(e):
                return False
            return True

    minifier(
        mod,
        args,
        module_fails=functools.partial(module_fails, check_str=options.check_str),
        dump_state=functools.partial(
            dump_compiler_graph_state,
            compiler_name=compiler_name,
        ),
        save_dir=options.save_dir,
        offload_to_disk=options.offload_to_disk,
        skip_offload=options.skip_saving_eager_intermediates,
        skip_sanity=options.skip_sanity,
        max_granularity=options.max_granularity,
    )


def run_repro(
    exported_program,
    # load_args,
    # kwargs: Dict[str, Any],
    *,
    config_patches: Optional[Dict[str, str]] = None,
    command="run",
    accuracy: Union[bool, str] = "",
    save_dir=None,
    tracing_mode=None,
    check_str=None,
    **more_kwargs,
):
    for k in more_kwargs:
        log.warning(
            "Unrecognized kwarg %s; perhaps this repro was made on a newer version of PyTorch",
            k,
        )

    if accuracy is True:
        accuracy = "accuracy"
        raise NotImplementedError("check for accuracy is not supported yet")
    elif accuracy is False:
        accuracy = ""

    parser = argparse.ArgumentParser(
        description=f"""\
An AOTI repro script, typically triggering a bug in PyTorch AOTInductor.
When run with no arguments, this script defaults to running '{command}'.
Extra flags may be available; to find out more, try '{command} --help'.
There are also alternate subcommands available, see below.

default settings on this script:
  {accuracy=}
  {tracing_mode=}
  {save_dir=}
  {check_str=}
""",
        formatter_class=argparse.RawTextHelpFormatter,
    )

    def common_flags(parser):
        parser.add_argument(
            "--save-dir",
            type=str,
            default=save_dir,
            metavar="DIR",
            help="directory where saved inputs live",
        )
        parser.add_argument(
            "--no-save-dir",
            dest="save_dir",
            action="store_const",
            const=None,
            help="don't use any directory for saved inputs",
        )
        # parser.add_argument(
        #     "--device",
        #     type=str,
        #     default="cpu",
        #     help="The device used in _export.aot_load. Default is cpu.",
        # )

    subparsers = parser.add_subparsers(
        dest="command", metavar="{run,minify,analyze}", required=True
    )

    parser_run = subparsers.add_parser(
        "run",
        help="just run the repro",
    )
    common_flags(parser_run)

    parser_minify = subparsers.add_parser(
        "minify", help="run the minifier on the repro"
    )
    common_flags(parser_minify)
    parser_get_args = subparsers.add_parser("get_args", help="get the args")
    common_flags(parser_get_args)
    parser_minify.add_argument(
        "--skip-saving-eager-intermediates",
        action="store_true",
        help="skip saving eager intermediates on --minify",
    )
    parser_minify.add_argument(
        "--offload-to-disk",
        action="store_true",
        help="during minification, offload delta debugging intermediates to disk.  Use if you're OOMing",
    )
    parser_minify.add_argument(
        "--skip-sanity",
        action="store_true",
        help="skip sanity check at beginning of minification on original graph",
    )
    parser_minify.add_argument(
        "--max-granularity",
        type=int,
        default=None,
        help="start at this granularity and work down; must be power of 2",
    )
    parser_minify.add_argument(
        "--check-str",
        type=str,
        default=check_str,
        help="require minified program to fail with error containing this string",
    )

    # Run the repro in the context of minification, inverting exit code meaning
    parser_minifier_query = subparsers.add_parser(
        "minifier-query",
    )
    common_flags(parser_minifier_query)
    parser_minifier_query.add_argument(
        "--check-str",
        type=str,
        default=check_str,
        help="require minified program to fail with error containing this string",
    )

    args = None
    if len(sys.argv) <= 1:
        args = [command, *sys.argv[1:]]

    options = parser.parse_args(args)
    COMMAND_FNS = {
        "minify": repro_minify,
        "run": repro_run,
        "get_args": repro_get_args,
    }
    return COMMAND_FNS[options.command](
        options, exported_program, config_patches=config_patches
    )
