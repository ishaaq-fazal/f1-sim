"""Every message from the browser is validated against these models; anything else is rejected."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

DriverCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
TeamId = Annotated[str, Field(pattern=r"^[a-z]{2,20}$")]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=64)


TrackId = Annotated[str, Field(pattern=r"^[a-z]{2,20}$")]
Compound = Literal["S", "M", "H"]


class StartCmd(_Msg):
    type: Literal["start"]
    team: TeamId
    laps: int = Field(ge=5, le=70)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    track: TrackId = "harbourline"
    # Starting tyre for each of your two cars, keyed by driver code.
    tyres: dict[DriverCode, Compound] | None = Field(default=None, max_length=2)


class SpeedCmd(_Msg):
    type: Literal["speed"]
    value: Literal[1, 2, 5, 10, 20, 50]


class PauseCmd(_Msg):
    type: Literal["pause"]
    paused: bool


class PitCmd(_Msg):
    type: Literal["pit"]
    driver: DriverCode
    compound: Literal["S", "M", "H"] | None


class ModeCmd(_Msg):
    type: Literal["mode"]
    driver: DriverCode
    mode: Literal["push", "normal", "conserve"]


Command = Annotated[Union[StartCmd, SpeedCmd, PauseCmd, PitCmd, ModeCmd], Field(discriminator="type")]
command_adapter: TypeAdapter[Command] = TypeAdapter(Command)


class DriverOut(BaseModel):
    code: str
    name: str


class TeamOut(BaseModel):
    id: str
    name: str
    color: str
    drivers: list[DriverOut]


class TrackOut(BaseModel):
    id: str
    name: str
    length_m: int
    base_lap_s: float
    wear: float
    overtaking: float
    blurb: str
    outline: list[list[float]]     # ~120 points for a preview


class SetupOut(BaseModel):
    teams: list[TeamOut]
    tracks: list[TrackOut]
    laps_default: int
    speeds: list[int]


# ------------------------------------------------------------------ accounts
# Response models list exactly what may leave the server: never password hashes or token versions.
from datetime import datetime  # noqa: E402

from pydantic import EmailStr, field_validator  # noqa: E402


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignupIn(_In):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class LoginIn(_In):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class RoleIn(_In):
    role: Literal["viewer", "principal", "admin"]


class UserOut(BaseModel):
    id: int
    email: str
    role: str


class RaceOut(BaseModel):
    id: int
    team: str
    track: str
    laps: int
    seed: int | None
    best_position: int | None
    winner: str | None
    summary: str
    created_at: datetime
