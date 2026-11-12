"""Default grid: original, made-up teams and drivers.

Swap this for real entries later (e.g. loaded with FastF1).
pace_s is the lap time offset from the reference lap (negative = faster).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Driver:
    code: str
    name: str
    team: str
    color: str
    pace_s: float          # team car pace + driver skill, s/lap vs reference
    consistency_s: float   # lap-to-lap noise (std dev)
    racecraft: float       # 0..1, helps attacking and defending


# (team id, display name, colour, car pace)
TEAMS = [
    ("aurora", "Aurora Racing", "#ff8a00", -0.30),
    ("vantage", "Vantage GP", "#2f8cff", -0.26),
    ("crimson", "Crimson Arc", "#e8263f", -0.24),
    ("verdant", "Verdant", "#1fbf6a", -0.15),
    ("nimbus", "Nimbus Motorsport", "#a970ff", -0.10),
    ("polaris", "Polaris", "#14c4b4", -0.05),
    ("solace", "Solace", "#ffd400", 0.00),
    ("halcyon", "Halcyon", "#ff6fb5", 0.04),
    ("ironforge", "Ironforge", "#9aa3ad", 0.09),
    ("kestrel", "Kestrel", "#c9a464", 0.14),
]

# (code, name, team id, driver delta, consistency, racecraft)
DRIVERS = [
    ("MAR", "Kai Marlow", "aurora", -0.10, 0.20, 0.85), ("OKA", "Dami Okafor", "aurora", 0.10, 0.24, 0.70),
    ("BRA", "Lena Brandt", "vantage", -0.15, 0.18, 0.80), ("VEN", "Saul Ventura", "vantage", 0.12, 0.26, 0.60),
    ("TAK", "Ren Takeda", "crimson", -0.05, 0.22, 0.75), ("ILV", "Mart Ilves", "crimson", 0.08, 0.25, 0.65),
    ("CAS", "Joel Castell", "verdant", 0.00, 0.22, 0.70), ("NOV", "Ida Novak", "verdant", 0.05, 0.23, 0.55),
    ("ROS", "Theo Rossard", "nimbus", -0.05, 0.27, 0.60), ("AMA", "Yusuf Amari", "nimbus", 0.10, 0.25, 0.50),
    ("LIN", "Mei Lindqvist", "polaris", -0.02, 0.21, 0.65), ("FER", "Tomas Ferreira", "polaris", 0.10, 0.28, 0.55),
    ("HAL", "Odin Hale", "solace", 0.00, 0.24, 0.60), ("DUV", "Claire Duval", "solace", 0.08, 0.26, 0.50),
    ("PET", "Nico Petrov", "halcyon", -0.03, 0.25, 0.55), ("SAN", "Ana Santos", "halcyon", 0.10, 0.27, 0.45),
    ("GRA", "Will Grady", "ironforge", 0.00, 0.28, 0.50), ("MOR", "Pia Moreau", "ironforge", 0.12, 0.30, 0.45),
    ("KOV", "Ivan Kovac", "kestrel", 0.00, 0.30, 0.50), ("ELL", "Sam Ellery", "kestrel", 0.15, 0.32, 0.40),
]


def default_grid() -> list[Driver]:
    teams = {t[0]: t for t in TEAMS}
    out = []
    for code, name, team, delta, cons, craft in DRIVERS:
        _, team_name, color, car = teams[team]
        out.append(Driver(code, name, team, color, round(car + delta, 3), cons, craft))
    return out
