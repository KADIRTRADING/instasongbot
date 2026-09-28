"""FSM states for the admin panel.

Unlike the user-facing flows (recognize/download/convert — deliberately
content-type-routed, no FSM, see ARCHITECTURE.md and handlers/*.py
docstrings), the admin panel genuinely has multi-step "tell me one thing,
then tell me another" conversations (pick a media type, THEN send the new
template text) where the second message's *meaning* depends entirely on
what was picked in the first step. That's exactly what FSM states are for,
and admin traffic is low-volume/trusted, so the added complexity is
justified only here.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminCaptionStates(StatesGroup):
    waiting_for_template_text = State()


class AdminButtonStates(StatesGroup):
    waiting_for_media_type = State()
    waiting_for_label = State()
    waiting_for_url = State()


class AdminLimitStates(StatesGroup):
    waiting_for_limit_value = State()


class AdminBroadcastStates(StatesGroup):
    waiting_for_text = State()
    waiting_for_confirmation = State()
