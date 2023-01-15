# Copyright 2013-2023 Lawrence Livermore National Security, LLC and other
# Spack Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: (Apache-2.0 OR MIT)

from __future__ import print_function

import argparse
import re
import sys

import llnl.util.tty as tty
import llnl.util.tty.color as color

import spack
import spack.cmd
import spack.cmd.common.arguments as arguments
import spack.config
import spack.environment
import spack.hash_types as ht
import spack.package_base
import spack.solver.asp as asp

description = "concretize a specs using an ASP solver"
section = "developer"
level = "long"

#: output options
show_options = ("asp", "opt", "output", "solutions")


def setup_parser(subparser):
    # Solver arguments
    subparser.add_argument(
        "--show",
        action="store",
        default="opt,solutions",
        help="select outputs: comma-separated list of: \n"
        "  asp          asp program text\n"
        "  opt          optimization criteria for best model\n"
        "  output       raw clingo output\n"
        "  solutions    models found by asp program\n"
        "  all          all of the above",
    )

    # Below are arguments w.r.t. spec display (like spack spec)
    arguments.add_common_arguments(subparser, ["long", "very_long", "install_status"])
    subparser.add_argument(
        "-y",
        "--yaml",
        action="store_const",
        dest="format",
        default=None,
        const="yaml",
        help="print concrete spec as yaml",
    )
    subparser.add_argument(
        "-j",
        "--json",
        action="store_const",
        dest="format",
        default=None,
        const="json",
        help="print concrete spec as json",
    )
    subparser.add_argument(
        "-c",
        "--cover",
        action="store",
        default="nodes",
        choices=["nodes", "edges", "paths"],
        help="how extensively to traverse the DAG (default: nodes)",
    )
    subparser.add_argument(
        "-N",
        "--namespaces",
        action="store_true",
        default=False,
        help="show fully qualified package names",
    )
    subparser.add_argument(
        "-t", "--types", action="store_true", default=False, help="show dependency types"
    )
    subparser.add_argument(
        "--timers",
        action="store_true",
        default=False,
        help="print out timers for different solve phases",
    )
    subparser.add_argument(
        "--stats", action="store_true", default=False, help="print out statistics from clingo"
    )
    subparser.add_argument("specs", nargs=argparse.REMAINDER, help="specs of packages")

    spack.cmd.common.arguments.add_concretizer_args(subparser)


def _process_result(result, show, required_format, kwargs):
    result.raise_if_unsat()
    opt, *_ = min(result.answers)

    # dump the solutions as concretized specs
    if ("opt" in show) and (not required_format):
        tty.msg("Best of %d considered solutions." % result.nmodels)
        tty.msg("Optimization Criteria:")

        maxlen = max(len(name) for name in result.criteria)
        max_depth = max(len(v) for v in result.criteria.values() if isinstance(v, list))

        header = "@*{"
        header += "".join(f"{depth:<4}" for depth in range(max_depth))
        header += "Criterion}"
        color.cprint(header)

        # make non-zero numbers red
        def highlight(n, c):
            return color.colorize(f"@{c}{{{n:<4}}}" if n > 0 else f"{n:<4}")

        for i, (name, cost) in enumerate(result.criteria.items(), 1):
            colored_name = name.replace("build:", "@c{build:}")
            colored_name = colored_name.replace("reuse:", "@B{reuse:}")
            colored_name = colored_name.replace("fixed:", "@G{fixed:}")
            colored_name = color.colorize(colored_name)

            if isinstance(cost, int):
                print(highlight(cost, "G") + "    " * (max_depth - 1) + colored_name)
            else:
                print(
                    "".join(highlight(c, "c" if "build:" in name else "B") for c in cost)
                    + colored_name
                )

        print()

    if "solutions" in show:
        for spec in result.specs:
            # With -y, just print YAML to output.
            if required_format == "yaml":
                # use write because to_yaml already has a newline.
                sys.stdout.write(spec.to_yaml(hash=ht.dag_hash))
            elif required_format == "json":
                sys.stdout.write(spec.to_json(hash=ht.dag_hash))
            else:
                sys.stdout.write(spec.tree(color=sys.stdout.isatty(), **kwargs))
        print()

    if result.unsolved_specs and "solutions" in show:
        tty.msg("Unsolved specs")
        for spec in result.unsolved_specs:
            print(spec)
        print()


def solve(parser, args):
    # these are the same options as `spack spec`
    name_fmt = "{namespace}.{name}" if args.namespaces else "{name}"
    fmt = "{@version}{%compiler}{compiler_flags}{variants}{arch=architecture}"
    install_status_fn = spack.spec.Spec.install_status
    kwargs = {
        "cover": args.cover,
        "format": name_fmt + fmt,
        "hashlen": None if args.very_long else 7,
        "show_types": args.types,
        "status_fn": install_status_fn if args.install_status else None,
        "hashes": args.long or args.very_long,
    }

    # process output options
    show = re.split(r"\s*,\s*", args.show)
    if "all" in show:
        show = show_options
    for d in show:
        if d not in show_options:
            raise ValueError(
                "Invalid option for '--show': '%s'\nchoose from: (%s)"
                % (d, ", ".join(show_options + ("all",)))
            )

    # Format required for the output (JSON, YAML or None)
    required_format = args.format

    # If we have an active environment, pick the specs from there
    env = spack.environment.active_environment()
    if env and args.specs:
        msg = "cannot give explicit specs when an environment is active"
        raise RuntimeError(msg)

    specs = list(env.user_specs) if env else spack.cmd.parse_specs(args.specs)

    solver = asp.Solver()
    output = sys.stdout if "asp" in show else None
    setup_only = set(show) == {"asp"}
    unify = spack.config.get("concretizer:unify")
    if unify != "when_possible":
        # set up solver parameters
        # Note: reuse and other concretizer prefs are passed as configuration
        result = solver.solve(
            specs,
            out=output,
            timers=args.timers,
            stats=args.stats,
            setup_only=setup_only,
        )
        if not setup_only:
            _process_result(result, show, required_format, kwargs)
    else:
        for idx, result in enumerate(
            solver.solve_in_rounds(specs, out=output, timers=args.timers, stats=args.stats)
        ):
            if "solutions" in show:
                tty.msg("ROUND {0}".format(idx))
                tty.msg("")
            else:
                print("% END ROUND {0}\n".format(idx))
            if not setup_only:
                _process_result(result, show, required_format, kwargs)
