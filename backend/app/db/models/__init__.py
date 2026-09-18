"""ORM models, one module per DATABASE.md section. Importing this package
registers every mapped class on `Base.metadata`, which is what Alembic
autogenerate and `Base.metadata.create_all()` (tests) walk."""

from app.db.models.season import Competitor, Season, SeasonCompetitor, SeasonRules, Week
from app.db.models.markets import CheckpointRun, Game, MarketSnapshot, Player, PropMarket, PropQuote
from app.db.models.forecast_lab import (
    AgentSession,
    AgentSessionEvidenceSnapshot,
    BenchmarkSlateFixture,
    BenchmarkSlatePlan,
    BenchmarkSlot,
    EvidenceSnapshot,
    ForecastObservation,
)
from app.db.models.competition import PassDecision, StakeRecommendation, Ticket, Wager
from app.db.models.settlement import BankrollTransaction, ResearchSettlement, Settlement
from app.db.models.events import CompetitionEvent
from app.db.models.ingestion import IngestionRun, ProviderCall
from app.db.models.roster import GamePlayer, GamePlayerObservation

__all__ = [
    "Season",
    "SeasonRules",
    "Week",
    "Competitor",
    "SeasonCompetitor",
    "Game",
    "Player",
    "PropMarket",
    "PropQuote",
    "MarketSnapshot",
    "CheckpointRun",
    "EvidenceSnapshot",
    "ForecastObservation",
    "BenchmarkSlatePlan",
    "BenchmarkSlot",
    "AgentSession",
    "AgentSessionEvidenceSnapshot",
    "BenchmarkSlateFixture",
    "StakeRecommendation",
    "Ticket",
    "Wager",
    "PassDecision",
    "Settlement",
    "ResearchSettlement",
    "BankrollTransaction",
    "CompetitionEvent",
    "IngestionRun",
    "ProviderCall",
    "GamePlayer",
    "GamePlayerObservation",
]
