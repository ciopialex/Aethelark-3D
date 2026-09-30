"""
Aethelark-3D Conversational Naming Decision Graph.
Guides the user through multi-turn, multi-modal printer identification and callsign assignment.
Supports mid-flight modality switching (Lights ➔ Bed Tap ➔ Toolhead Wave ➔ Chime).
"""

import json
from enum import Enum
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field

from aethelark3d.config import config
from aethelark3d.beacon import PrinterBeacon, BeaconMode, TapDetectionResult
from aethelark3d.aliases import PrinterAliasManager


class NamingStep(str, Enum):
    INIT = "init"                                  # Propose modalities
    MODE_SELECTED = "mode_selected"                # User picked modality
    BEACONING_PRINTER = "beaconing_printer"        # Active printer beaconed, awaiting name
    AWAITING_TAP = "awaiting_tap"                  # Listening for bed tap
    COMPLETED = "completed"                        # All requested printers named


@dataclass
class NamingSessionState:
    session_id: str
    current_step: NamingStep = NamingStep.INIT
    active_mode: BeaconMode = BeaconMode.SPOTLIGHT
    unnamed_printers: List[str] = field(default_factory=list)
    named_printers: Dict[str, str] = field(default_factory=dict)  # {ALIAS: PRINTER_KEY}
    active_printer_key: Optional[str] = None
    transcript: List[Dict[str, str]] = field(default_factory=list)


class NamingSessionGraph:
    """
    Decisional State Machine Graph for conversational fleet naming.
    """

    def __init__(self, session_id: str = "fleet_naming_default"):
        self.state = NamingSessionState(
            session_id=session_id,
            unnamed_printers=list(config.get_fleet().keys())
        )

    def start_session(self) -> Dict[str, Any]:
        """
        Gate 1: Welcome user and propose multi-modal physical identification options.
        """
        self.state.current_step = NamingStep.INIT
        self.state.unnamed_printers = list(config.get_fleet().keys())
        self.state.named_printers = {}

        options = [
            {"mode": "spotlight", "name": "💡 Spotlight Lights", "desc": "Turns off all lights except one, highlighting each machine sequentially (Best for enclosed CC1/CC2/Bambu)."},
            {"mode": "tap", "name": "👆 Touch Bed Tap", "desc": "Walk over and tap the bed/probe of any machine to claim and name it instantly."},
            {"mode": "wave", "name": "👋 Robotic Wave", "desc": "Wiggles the toolhead left-right (100% universal on all printers including open-frame A1/Neptune)."},
            {"mode": "chime", "name": "🎵 Stepper Chime", "desc": "Plays an acoustic 3-note chime on the stepper motors."}
        ]

        return {
            "step": self.state.current_step.value,
            "unnamed_count": len(self.state.unnamed_printers),
            "unnamed_printers": self.state.unnamed_printers,
            "options": options,
            "speech_prompt": (
                f"I found {len(self.state.unnamed_printers)} printers in your fleet. "
                "How would you like to identify them? We can use Spotlight Lights, Touch Bed Tap, Robotic Toolhead Wave, or Stepper Chimes."
            )
        }

    async def select_mode(self, mode_str: str, use_simulator: bool = False) -> Dict[str, Any]:
        """
        Gate 2: User selects mode (or filters subset). Trigger the first beacon.
        """
        clean_mode = mode_str.lower().strip()
        if "light" in clean_mode or "spot" in clean_mode or "encase" in clean_mode or "enclosed" in clean_mode:
            self.state.active_mode = BeaconMode.SPOTLIGHT
        elif "tap" in clean_mode or "touch" in clean_mode or "sensor" in clean_mode:
            self.state.active_mode = BeaconMode.TAP
        elif "wave" in clean_mode or "wiggle" in clean_mode or "move" in clean_mode:
            self.state.active_mode = BeaconMode.WAVE
        elif "chime" in clean_mode or "sound" in clean_mode or "music" in clean_mode or "sing" in clean_mode:
            self.state.active_mode = BeaconMode.CHIME
        else:
            self.state.active_mode = BeaconMode.SPOTLIGHT

        if self.state.active_mode == BeaconMode.TAP:
            self.state.current_step = NamingStep.AWAITING_TAP
            return {
                "step": self.state.current_step.value,
                "mode": self.state.active_mode.value,
                "speech_prompt": "Touch mode active! Walk over and tap the build bed or probe on any printer, and tell me what to name it."
            }

        # For Spotlight / Wave / Chime: Beacon the first unnamed printer
        return await self.beacon_next_printer(use_simulator=use_simulator)

    async def beacon_next_printer(self, use_simulator: bool = False) -> Dict[str, Any]:
        """
        Gate 3: Activate beacon on the next unnamed printer in sequence.
        """
        if not self.state.unnamed_printers:
            return self.finalize_session()

        target_printer = self.state.unnamed_printers[0]
        self.state.active_printer_key = target_printer
        self.state.current_step = NamingStep.BEACONING_PRINTER

        # Trigger physical beacon
        if self.state.active_mode == BeaconMode.SPOTLIGHT:
            await PrinterBeacon.isolate_spotlight(target_printer, use_simulator=use_simulator)
            prompt = f"I've lit up the spotlight on printer {target_printer}. What name should we give it?"
        elif self.state.active_mode == BeaconMode.WAVE:
            await PrinterBeacon.trigger_wave(target_printer, use_simulator=use_simulator)
            prompt = f"Look at the printer waving its toolhead at you ({target_printer}). What's its callsign?"
        elif self.state.active_mode == BeaconMode.CHIME:
            await PrinterBeacon.trigger_chime(target_printer, note_freq=880, use_simulator=use_simulator)
            prompt = f"The printer that just chimed ({target_printer})—what do you want to call it?"
        else:
            prompt = f"Ready to name printer {target_printer}. What's the callsign?"

        return {
            "step": self.state.current_step.value,
            "mode": self.state.active_mode.value,
            "active_printer": target_printer,
            "remaining_count": len(self.state.unnamed_printers),
            "speech_prompt": prompt
        }

    async def assign_name_and_advance(
        self,
        alias_name: str,
        printer_key: Optional[str] = None,
        use_simulator: bool = False
    ) -> Dict[str, Any]:
        """
        Gate 4: Save the user's chosen name, remove from unnamed pool, and advance to next printer.
        """
        target_key = printer_key or self.state.active_printer_key
        if not target_key and self.state.unnamed_printers:
            target_key = self.state.unnamed_printers[0]

        if not target_key:
            return self.finalize_session()

        clean_alias = alias_name.strip().upper()
        clean_key = target_key.strip().upper().replace("-", "_")

        # Persist alias
        PrinterAliasManager.set_alias(clean_alias, clean_key)
        self.state.named_printers[clean_alias] = clean_key

        if clean_key in self.state.unnamed_printers:
            self.state.unnamed_printers.remove(clean_key)

        ack = f"Saved callsign '{clean_alias}' for {clean_key}."

        if not self.state.unnamed_printers:
            fin = self.finalize_session()
            fin["speech_prompt"] = f"{ack} That was the last printer! Your entire fleet is named and registered."
            return fin

        next_beacon = await self.beacon_next_printer(use_simulator=use_simulator)
        next_beacon["speech_prompt"] = f"{ack} Moving to the next one... {next_beacon['speech_prompt']}"
        return next_beacon

    async def switch_mode(self, new_mode_str: str, use_simulator: bool = False) -> Dict[str, Any]:
        """
        Gate 5: Mid-flight modality switch (e.g. user says 'Now for my open-frame A1 let's use the wave').
        """
        return await self.select_mode(new_mode_str, use_simulator=use_simulator)

    def finalize_session(self) -> Dict[str, Any]:
        """
        Final Gate: Turn off beacons, print summary card, and finish session.
        """
        self.state.current_step = NamingStep.COMPLETED
        all_aliases = PrinterAliasManager.get_aliases()

        return {
            "step": self.state.current_step.value,
            "status": "COMPLETED",
            "fleet_aliases": all_aliases,
            "speech_prompt": f"Fleet naming complete! I've registered {len(all_aliases)} printer callsigns."
        }
