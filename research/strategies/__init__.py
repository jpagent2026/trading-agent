from .ema_pullback import EmaSmaPullback
from .harry_sharp import HarrySharpWick
from .orb import ORB

REGISTRY = {s.name: s for s in (ORB, HarrySharpWick, EmaSmaPullback)}


def get(name: str):
    return REGISTRY[name]()


def all_strategies():
    return [cls() for cls in REGISTRY.values()]
