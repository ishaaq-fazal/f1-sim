"""Every message from the browser is validated against these models; anything else is rejected."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

DriverCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
TeamId = Annotated[str, Field(pattern=r"^[a-z]{2,20}$")]


class _Msg(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=64)


TrackId = Annotated[str, Field(pattern=r"^[a-z]{2,20}$")]
Compound = Literal["S", "M", "H", "I", "W"]


class StartCmd(_Msg):
    type: Literal["start"]
    team: TeamId
    laps: int = Field(ge=5, le=70)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    track: TrackId = "harbourline"
    weather: Literal["dry", "wet", "changeable"] = "dry"
    # Starting tyre for each of your two cars, keyed by driver code.
    tyres: dict[DriverCode, Compound] | None = Field(default=None, max_length=2)
    # Run the next round of one of your seasons. The season fixes the team, circuit, laps and weather.
    season: int | None = Field(default=None, ge=1, le=2**31 - 1)


class SpeedCmd(_Msg):
    type: Literal["speed"]
    value: Literal[1, 2, 5, 10, 20, 50]


class PauseCmd(_Msg):
    type: Literal["pause"]
    paused: bool


class PitCmd(_Msg):
    type: Literal["pit"]
    driver: DriverCode
    compound: Literal["S", "M", "H", "I", "W"] | None


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
    schedule: list[str]            # season calendar: circuit id for each round


# ------------------------------------------------------------------ accounts
# Response models list exactly what may leave the server: never password hashes or token versions.
from datetime import datetime  # noqa: E402

from pydantic import EmailStr, field_validator, model_validator  # noqa: E402


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# Passwords of 10+ characters that are still among the most guessed. Checked case-insensitively.
_COMMON_PASSWORDS = {
    "1234567890", "12345678910", "0123456789", "0987654321", "1111111111", "123456789a",
    "password12", "password123", "password1234", "passw0rd123", "qwertyuiop", "qwerty1234",
    "qwerty12345", "1q2w3e4r5t", "1qaz2wsx3edc", "asdfghjkl1", "zxcvbnm123", "iloveyou12",
    "letmein123", "welcome123", "admin12345", "abc1234567", "abcdefghij", "football12",
    "liverpool1", "chelsea123", "arsenal123", "manchester", "formula1f1", "ferrari123",
    "lewishamilton", "maxverstappen", "changeme12", "trustno1234",
}


class SignupIn(_In):
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

    @field_validator("email")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()

    @model_validator(mode="after")
    def _strong_enough(self):
        pw = self.password.lower()
        if pw in _COMMON_PASSWORDS:
            raise ValueError("That password is too common")
        if len(set(pw)) < 4:
            raise ValueError("Use a password with more variety")
        local = self.email.split("@")[0]
        if len(local) >= 4 and local in pw:
            raise ValueError("Don't use your email in your password")
        return self


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


# ------------------------------------------------------------------ seasons
class SeasonIn(_In):
    team: TeamId
    laps: int = Field(ge=5, le=70)
    weather: Literal["dry", "wet", "changeable"] = "dry"


class SeasonOut(BaseModel):
    id: int
    team: str
    laps: int
    weather: str
    rounds_done: int
    finished: bool
    created_at: datetime


class RoundOut(BaseModel):
    round: int
    track: str
    done: bool
    winner: str | None = None        # driver code
    your_points: int | None = None


class DriverStandingOut(BaseModel):
    pos: int
    code: str
    name: str
    team: str
    points: int
    wins: int
    podiums: int


class TeamStandingOut(BaseModel):
    pos: int
    team: str
    name: str
    points: int
    wins: int


class ForecastSlotOut(BaseModel):
    in_min: int
    chance: int


class ForecastOut(BaseModel):
    summary: str
    rain_chance: int
    wet_start: bool
    track_wetness: float
    next_20_min: list[ForecastSlotOut]


class NextRoundOut(BaseModel):
    round: int
    track: str
    laps: int
    forecast: ForecastOut


class SeasonDetailOut(BaseModel):
    season: SeasonOut
    points_system: list[int]
    rounds: list[RoundOut]
    drivers: list[DriverStandingOut]
    teams: list[TeamStandingOut]


# ------------------------------------------------------------------ careers
class CareerIn(_In):
    team: TeamId
    laps: int = Field(ge=5, le=70)
    weather: Literal["dry", "wet", "changeable"] = "dry"


class UpgradeIn(_In):
    area: Literal["power", "aero", "tyres", "reliability"]


class SignIn(_In):
    code: DriverCode
    replace: DriverCode
    years: int = Field(ge=1, le=3)


class RenewIn(_In):
    code: DriverCode
    years: int = Field(ge=1, le=3)


class ContractOut(BaseModel):
    code: str
    name: str
    rating: int
    salary: float
    years: int


class UpgradeOut(BaseModel):
    area: str
    label: str
    level: int
    next_cost: float | None


class CareerSeasonOut(BaseModel):
    season: int
    team_position: int
    points: int
    champion: str
    prize: float
    salaries: float


class CareerOut(BaseModel):
    id: int
    team: str
    laps: int
    weather: str
    season_no: int
    season_id: int
    rounds_done: int
    season_finished: bool
    budget: float
    income: float
    spent: dict[str, float]
    upgrades: list[UpgradeOut]
    drivers: list[ContractOut]
    market: list[ContractOut]
    history: list[CareerSeasonOut]
    ready: bool
    blocker: str | None
    created_at: datetime
