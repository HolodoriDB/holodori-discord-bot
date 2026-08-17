"""pydantic models mirroring the holodori api json (camelCase to match the payload).

extra fields are ignored so the api can add keys without breaking us.
"""

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CardGroup(_Model):
    name: str
    color1: str | None = None
    color2: str | None = None


class SongDifficulty(_Model):
    type: str
    level: int
    color: str | None = None


class Card(_Model):
    id: str
    name: str
    character: str
    characterId: str
    rarity: int
    attribute: int | None = None
    attributeName: str | None = None
    attributeIcon: str | None = None
    mainStat: str | None = None
    image: str | None = None
    thumb: str | None = None
    group: CardGroup | None = None
    groups: list[CardGroup] = []
    order: int = 0
    maxLevel: int | None = None
    maxBloom: int | None = None
    levelLimits: list[int] = []


class CardSkill(_Model):
    type: str  # passive | active | special
    descriptions: list[str] = []
    bloomUnlock: int | None = None
    icon: str | None = None


class CardVoiceline(_Model):
    type: str
    text: str | None = None
    audio: str | None = None


class CardDetail(Card):
    animated: bool = False
    cardVideo: str | None = None
    skills: list[CardSkill] = []
    voicelines: list[CardVoiceline] = []
    weights: dict[str, int] | None = None
    baseTotals: list[int] = []


class Song(_Model):
    id: str
    title: str
    jacket: str | None = None
    difficulties: list[SongDifficulty] = []
    startTime: int | None = None
    length: int | None = None
    category: str | None = None
    mutedInStreamerMode: bool = False
    autoGiven: bool = False
    mainQuestUnlock: list | None = None
    holomemUnlock: str | None = None


class SongDifficultyDetail(_Model):
    difficultyType: str
    difficultyLevel: int
    chartAssetId: str | None = None
    fullComboNoteCount: int | None = None
    normalNoteCount: int | None = None
    chartHash: str | None = None


class SongDetail(_Model):
    id: str
    title: str
    titleRuby: str | None = None
    lyricist: str | None = None
    composer: str | None = None
    arranger: str | None = None
    jacketAssetId: str | None = None
    startTime: int | None = None
    playingSeconds: int | None = None
    categoryType: str | None = None
    mvUrl: str | None = None
    musicSingerType: str | None = None
    characterIds: list[str] = []
    characterGroupDisplayName: str | None = None
    chorusStartMillisecond: int | None = None
    chorusEndMillisecond: int | None = None
    difficulties: list[SongDifficultyDetail] = []
    liveRewards: dict | None = None
    obtain: dict | None = None


class Holomem(_Model):
    id: str
    name: str
    shortName: str | None = None
    icon: str | None = None
    order: int = 0


class HolomemSticker(_Model):
    name: str
    image: str | None = None


class HolomemMember(Holomem):
    stickers: list[HolomemSticker] = []


class HolomemGroup(_Model):
    id: str | None = None
    name: str
    members: list[HolomemMember] = []


class EventInfo(_Model):
    eventId: str
    name: str
    logo: str | None = None
    banner: str | None = None
    type: int | None = None
    startTime: int | None = None
    endTime: int | None = None
    aggregationStartTime: int | None = None
    revealStartTime: int | None = None
    isSongScore: bool = False
    chapters: list[str] = []
    live: bool = False
    hasData: bool = False


class AssetInfo(_Model):
    revision: int = 0
    cdn: str = ""
    hashes: dict[str, str] = {}
    manual: str | None = None


# manually-added search aliases (song / event / holomem share one shape; target_id is the string id)
class Alias(_Model):
    id: int
    target_id: str
    alias: str
    region: str | None = None
    created_at: str | None = None


class AliasesResponse(_Model):
    aliases: list[Alias] = []


class AliasAddResponse(_Model):
    success: bool = False
    id: int | None = None
