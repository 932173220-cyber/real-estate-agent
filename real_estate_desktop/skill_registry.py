"""Skill registry with lazy loading and per-skill tool scopes.

Progressive disclosure has three levels:

* L1 ``catalogue()`` — name plus one-line description, cheap enough to put in
  every routing prompt.
* L2 ``load_skill(name)`` — the full ``SKILL.md`` body, read on demand and
  cached on a singleton skill instance.
* L3 ``tools_for(names)`` — a skill's tool schemas are only exposed to the
  model after the host activates that skill for the current request.

Registration is dynamic: a deployment can register its own skill before the
runtime is built, and ``reset()`` makes the registry testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

CORE_TOOLS = ('think', 'load_skill')


@dataclass
class Skill:
    name: str
    description: str
    tools: tuple[str, ...] = ()
    path: Path | None = None
    _instructions: str | None = field(default=None, repr=False)

    @property
    def instructions(self) -> str:
        """Full instructions, read once per registered instance."""
        if self._instructions is None:
            if self.path is None or not self.path.is_file():
                raise ValueError(f'Skill {self.name} 缺少说明文件。')
            self._instructions = self.path.read_text('utf-8')
        return self._instructions

    def instance(self) -> "Skill":
        """Skills are stateless singletons; callers get the same object back."""
        return self


class SkillRegistry:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = Path(root) if root else Path(__file__).parent / 'skills'
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill, *, replace: bool = False) -> Skill:
        if not skill.name or not skill.description:
            raise ValueError('Skill 需要名称和说明。')
        if skill.name in self._skills and not replace:
            raise ValueError(f'Skill {skill.name} 已注册。')
        self._skills[skill.name] = skill
        return skill.instance()

    def unregister(self, name: str) -> None:
        self._skills.pop(name, None)

    def reset(self) -> None:
        self._skills.clear()

    def __contains__(self, name) -> bool:
        return name in self._skills

    def __len__(self) -> int:
        return len(self._skills)

    def __iter__(self):
        return iter(self._skills.values())

    def names(self) -> list[str]:
        return list(self._skills)

    def get(self, name: str) -> Skill:
        skill = self._skills.get(name)
        if skill is None:
            raise ValueError('未知Skill')
        return skill.instance()

    def catalogue(self) -> dict[str, str]:
        return {skill.name: skill.description for skill in self._skills.values()}

    def legacy_catalogue(self) -> dict[str, tuple[str, str]]:
        """``{name: (title, description)}`` for callers that render a title."""
        return {skill.name: (skill.name, skill.description) for skill in self._skills.values()}

    def tools_for(self, names) -> set[str]:
        tools = set(CORE_TOOLS)
        for name in names:
            skill = self._skills.get(name)
            if skill is not None:
                tools.update(skill.tools)
        return tools


registry = SkillRegistry()

_BUILTIN = (
    ('customer-memory', '提取、比较、确认并版本化当前客户的背景语义记忆与购房情景记忆',
     ('query_customer_memory',)),
    ('listing-search', '按结构化硬条件查询房源',
     ('search_listings', 'get_property_details')),
    ('property-community-rag', '检索候选房源关联的小区与区域资料',
     ('search_communities', 'retrieve_area_info')),
    ('recommendation', '分析候选并生成有据可查的推荐',
     ('draft_recommendation',)),
    ('report-followup', '经纪人确认、幂等保存与跟进',
     ()),
)

for _name, _description, _tools in _BUILTIN:
    registry.register(Skill(name=_name, description=_description, tools=_tools,
                            path=registry.root / _name / 'SKILL.md'))


def load_skill(name: str) -> str:
    return registry.get(name).instructions


def catalogue() -> dict[str, str]:
    return registry.catalogue()


# Snapshot for callers that render the built-in catalogue in one prompt.
CATALOGUE = registry.catalogue()


