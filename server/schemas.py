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
QualiMode = Literal["safe", "normal", "push"]


class StartCmd(_Msg):
    type: Literal["start"]
    team: TeamId
    laps: int = Field(ge=5, le=70)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    track: TrackId = "harbourline"
    weather: Literal["dry", "wet", "changeable", "showers", "drying"] = "dry"
    # Starting tyre for each of your two cars, keyed by driver code.
    tyres: dict[DriverCode, Compound] | None = Field(default=None, max_length=2)
    # Qualifying approach for each of your two cars: push (quicker, riskier) or play safe.
    quali: dict[DriverCode, QualiMode] | None = Field(default=None, max_length=2)
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


class OrderCmd(_Msg):
    """Team order: the car ahead lets its team-mate through (the driver may ignore it)."""
    type: Literal["order"]
    order: Literal["swap"]


class LobbyCmd(_Msg):
    """Open the race lobby of a league you're in (its next round is raced by everyone together)."""
    type: Literal["lobby"]
    league: int = Field(ge=1, le=2**31 - 1)


class ReadyCmd(_Msg):
    """Ready (or not) for the lobby's round, with your starting tyres. The race starts when every
    member is ready."""
    type: Literal["ready"]
    ready: bool
    tyres: dict[DriverCode, Compound] | None = Field(default=None, max_length=2)
    quali: dict[DriverCode, QualiMode] | None = Field(default=None, max_length=2)


Command = Annotated[Union[StartCmd, SpeedCmd, PauseCmd, PitCmd, ModeCmd, OrderCmd, LobbyCmd, ReadyCmd],
                    Field(discriminator="type")]
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
    sprint_rounds: list[int] = []  # rounds that are sprint weekends
    sprint_points: list[int] = []


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
    league_id: int | None = None     # set when this is your season in a league
    created_at: datetime


class RoundOut(BaseModel):
    round: int
    track: str
    done: bool
    winner: str | None = None        # driver code
    your_points: int | None = None   # Grand Prix and sprint together
    sprint: bool = False             # a sprint weekend
    sprint_done: bool = False
    sprint_winner: str | None = None
    your_sprint_points: int | None = None


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
    laps: int                        # laps of the session raced next
    forecast: ForecastOut
    session: Literal["race", "sprint"] = "race"
    sprint_weekend: bool = False


class SeasonDetailOut(BaseModel):
    season: SeasonOut
    points_system: list[int]
    sprint_points_system: list[int] = []
    next_session: Literal["race", "sprint"] | None = None
    rounds: list[RoundOut]
    drivers: list[DriverStandingOut]
    teams: list[TeamStandingOut]


# ------------------------------------------------------------------ careers
class CareerIn(_In):
    team: TeamId
    laps: int = Field(ge=5, le=70)
    weather: Literal["dry", "wet", "changeable"] = "dry"


class UpgradeIn(_In):
    area: Literal["power", "aero", "tyres", "pit", "reliability"]


class OfferIn(_In):
    team: TeamId


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
    age: int
    change: int            # rating change over the last off-season
    retiring: bool


class UpgradeOut(BaseModel):
    area: str
    label: str
    level: int
    next_cost: int | None      # development points


class ObjectiveOut(BaseModel):
    kind: str
    text: str
    now: str
    met: bool | None           # None before the first race


class ObjectiveResultOut(BaseModel):
    text: str
    met: bool


class OfferOut(BaseModel):
    team: str
    name: str


class CareerSeasonOut(BaseModel):
    season: int
    team: str = ""
    team_position: int
    points: int
    champion: str
    prize: float
    salaries: float
    dev_points: int = 0
    verdict: str = ""
    objectives: list[ObjectiveResultOut] = []


class CareerOut(BaseModel):
    id: int
    team: str
    laps: int
    weather: str
    season_no: int
    season_id: int
    rounds_done: int
    season_finished: bool
    next_session: Literal["race", "sprint"] | None = None
    budget: float
    income: float
    spent: dict[str, float]
    dev_points: int
    factory_dp: int
    factory_cost: float
    status: str                # active, sacked or over
    warning: bool              # the board warned you last season
    expected: int              # where the board expects you to finish
    objectives: list[ObjectiveOut]
    offers: list[OfferOut]
    teams_managed: list[str]
    upgrades: list[UpgradeOut]
    drivers: list[ContractOut]
    market: list[ContractOut]
    history: list[CareerSeasonOut]
    ready: bool
    blocker: str | None
    created_at: datetime


# ------------------------------------------------------------------ leagues
Nickname = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,18}[A-Za-z0-9_.]$")]
LeagueName = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.'&!\-]{0,38}[A-Za-z0-9_.'!]$")]
InviteCode = Annotated[str, Field(pattern=r"^[A-Za-z0-9]{8}$")]


class LeagueIn(_In):
    name: LeagueName
    laps: int = Field(ge=5, le=70)
    team: TeamId                 # the team you run in this league
    nickname: Nickname           # what other members see instead of your email


class JoinLeagueIn(_In):
    code: InviteCode
    team: TeamId
    nickname: Nickname


class LeagueOut(BaseModel):
    id: int
    name: str
    code: str                    # members can share it to invite friends
    laps: int
    members: int
    is_owner: bool
    your_nickname: str
    your_season_id: int
    created_at: datetime


class LeagueRowOut(BaseModel):
    pos: int
    nickname: str
    team: str
    team_name: str
    you: bool
    owner: bool
    rounds_done: int
    finished: bool
    points: int
    wins: int
    podiums: int
    best_finish: int | None
    member_id: int | None = None   # only shown to the league's owner, to remove someone


class LeagueDetailOut(BaseModel):
    league: LeagueOut
    rounds: int
    schedule: list[str]
    table: list[LeagueRowOut]
    sprint_rounds: list[int] = []
