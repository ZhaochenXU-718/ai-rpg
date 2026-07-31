"""Compile the author-private context handed to the narrator.

The narrator needs the story blueprint and the in-scene character cards to
portray characters and hold long-term direction. This channel is separate
from ``PerceptionSnapshot``: nothing here counts as player knowledge, and it
never enters action suggestions, fact extraction or memory compaction.
"""

from __future__ import annotations

from .content import Story
from .llm_protocol import (
    AuthorCharacterCard,
    CandidateModule,
    NarrativeAuthorContext,
)


_CARD_TEXT_FIELDS = (
    "motivation",
    "voice",
    "initial_relationship",
    "secret",
    "pressure",
    "behavior",
    "mannerisms",
    "narration_notes",
)


def _character_card(story: Story, character_id: str) -> AuthorCharacterCard | None:
    card = story.characters.get(character_id)
    if not isinstance(card, dict):
        return None
    fields = {
        key: " ".join(str(card.get(key) or "").split())
        for key in _CARD_TEXT_FIELDS
    }
    raw_examples = card.get("dialogue_examples")
    dialogue_examples = tuple(
        str(item).strip()
        for item in (raw_examples if isinstance(raw_examples, list) else [])
        if str(item).strip()
    )
    if not any(fields.values()) and not dialogue_examples:
        return None
    return AuthorCharacterCard(
        character_id=character_id,
        name=story.character_name(character_id),
        dialogue_examples=dialogue_examples,
        **fields,
    )


def build_author_context(
    story: Story,
    in_scene_character_ids: tuple[str, ...],
    *,
    candidate_modules: tuple[CandidateModule, ...] = (),
    include_opening_narration: bool = True,
) -> NarrativeAuthorContext | None:
    """Return the narrator-only author context, or None when nothing is authored."""
    cards = []
    for character_id in dict.fromkeys(in_scene_character_ids):
        if character_id == story.player_id:
            continue
        card = _character_card(story, character_id)
        if card is not None:
            cards.append(card)
    context = NarrativeAuthorContext(
        story_brief=story.ai_plot,
        emotional_contract=story.emotional_contract,
        opening_narration=(
            story.opening_narration if include_opening_narration else ""
        ),
        active_guidelines=story.narrative_guidelines,
        critical_reminders=story.critical_reminders,
        in_scene_character_cards=tuple(cards),
        candidate_modules=candidate_modules,
    )
    if (
        not context.story_brief
        and not context.emotional_contract
        and not context.opening_narration
        and not context.active_guidelines
        and not context.critical_reminders
        and not context.in_scene_character_cards
        and not context.candidate_modules
    ):
        return None
    return context
