import argparse

from rasa.cli.arguments.default_arguments import add_model_param
from rasa.cli.arguments.run import add_run_arguments


def set_shell_arguments(parser: argparse.ArgumentParser):
    add_run_arguments(parser)
    add_model_param(parser)


def set_shell_nlu_arguments(parser: argparse.ArgumentParser):
    add_model_param(parser)
