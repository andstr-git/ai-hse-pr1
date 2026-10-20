import json

import rules_pkg.sub

from . import sub
from .sub import target as run_target


def _helper(value):
    return value


def same_module(value):
    return _helper(value)  # rule: a name from the same module


def nested_outer():
    def nested_inner():
        return 1

    return nested_inner()  # rule: a nested function


def via_alias(value):
    return run_target(value)  # rule: from-import with an alias, relative


def via_module(value):
    return sub.target()  # rule: `from . import sub` + attribute


def via_absolute(value):
    return rules_pkg.sub.target()  # rule: `import pkg.sub` + attribute chain


class Maker:
    def decorate(self, value):
        return value

    def build(self, value):
        return self.decorate(value)  # rule: self.method() defined in the same class


def make():
    return Maker()  # rule: a class is called -> the class itself


def wrapper(target):
    return target()  # rule: a parameter shadows the module name -> unresolved


def local_shadow():
    run_target = 1
    return run_target()  # rule: assignment shadows the import -> unresolved


def external_call(value):
    return json.dumps(value)  # an external target


def unknown_call():
    return mystery()  # unresolved: unknown name  # noqa: F821
