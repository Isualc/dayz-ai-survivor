"""Offline regression scenarios: python -m unittest discover -s daemon -p test_survival.py."""

import copy
import json
import math
import os
import tempfile
import unittest
from unittest import mock

import survival
import tactics


def state(**vitals):
    npc = {"spawned": True, "alive": True, "name": "Viktor", "health": 100,
           "blood": 5000, "water": 4000, "energy": 5000, "stomach_volume": 0,
           "heat_comfort": 0, "pos_x": 1000, "pos_z": 1000, "in_hands": "",
           "bleeding": False, "disease": {"agents": {}, "sick": False}}
    npc.update(vitals)
    return {"seq": 1, "uptime": 100, "bridge_version": "0.9.0", "npc": npc,
            "inventory": [], "nearby": [], "command": {"id": "old", "status": "idle"}}


def item(cn, kind="other", quantity=1, **extra):
    return {"classname": cn, "kind": kind, "quantity": quantity, "health": 100, **extra}


def ground(cn, kind="item", distance=5, **extra):
    return {"classname": cn, "kind": kind, "distance": distance, "x": 1000,
            "z": 1000 + distance, **extra}


class FakeBridge:
    def __init__(self, snapshot, directory):
        self.snapshot, self.sent = snapshot, []
        self.dir, self.npc_id = directory, "offline"

    def read_state(self):
        return copy.deepcopy(self.snapshot)

    def send(self, action, timeout=.2, **params):
        assert timeout <= .2, "a routine tick must not block"
        command_id = f"c{len(self.sent) + 1}"
        self.sent.append((action, params))
        return command_id

    def advance(self, status="done", **updates):
        self.snapshot["seq"] += 1
        self.snapshot["uptime"] += 5
        self.snapshot["command"] = {"id": f"c{len(self.sent)}", "status": status,
                                    "action": self.sent[-1][0] if self.sent else ""}
        self.snapshot["npc"].update(updates)


class SurvivalPolicyTests(unittest.TestCase):
    def choose(self, snapshot, memory=None, explore=False):
        return survival.choose_action(snapshot, memory or survival._new_memory(), 1000, "Viktor", explore)

    def test_bleeding_precedes_food_and_weapon_loot(self):
        s = state(bleeding=True, energy=100)
        s["inventory"] = [item("BandageDressing"), item("Apple", "food")]
        s["nearby"] = [ground("M4A1")]
        result = self.choose(s)
        self.assertEqual(result["action"], "treat_other")
        self.assertEqual(result["params"], {"target": "Viktor", "item": "BandageDressing"})

    def test_critical_water_precedes_rifle_loot(self):
        s = state(water=200)
        s["nearby"] = [ground("Well_Pump", "water", 2), ground("M4A1")]
        self.assertEqual(self.choose(s)["action"], "drink_well")

    def test_hunger_does_not_consume_medicine(self):
        s = state(energy=200)
        s["inventory"] = [item("TetracyclineAntibiotics", "food", 8)]
        self.assertNotEqual(self.choose(s).get("action"), "eat")

    def test_food_stage_and_liquid_are_checked(self):
        for stage in (None, 1, 5, 6):
            self.assertFalse(survival.safe_food(item("DeerSteakMeat", "food", 100, food_stage=stage)))
        for stage in (2, 3, 4):
            self.assertTrue(survival.safe_food(item("DeerSteakMeat", "food", 100, food_stage=stage)))
        self.assertFalse(survival.safe_food(item("HumanSteakMeat", "food", 100, food_stage=2)))
        self.assertFalse(survival.safe_food(item("Apple", "food", 100, frozen=True)))
        self.assertFalse(survival.safe_food(item("Apple", "food", 100, food_safe=False)))
        self.assertFalse(survival.safe_drink(item("Canteen", "drink", 100)))
        self.assertFalse(survival.safe_drink(item("Canteen", "drink", 100, liquid_safe=False)))
        self.assertTrue(survival.safe_drink(item("Canteen", "drink", 100, liquid_safe=True)))

    def test_numeric_dayz_food_water_and_freezing_flags(self):
        s = state(water=200)
        s["inventory"] = [item("Canteen", "drink", 300, liquid_safe=1, frozen=0)]
        self.assertEqual(self.choose(s)["action"], "drink")
        self.assertTrue(survival.safe_food(item("Apple", "food", 100, food_safe=1, frozen=0)))
        for invalid in (0, "1", "false", 2, None):
            with self.subTest(invalid=invalid):
                self.assertFalse(survival.safe_drink(item("Canteen", "drink", 100, liquid_safe=invalid)))
                self.assertFalse(survival.safe_food(item("Apple", "food", 100, food_safe=invalid)))
        for frozen in (1, "0", "false", None):
            with self.subTest(frozen=frozen):
                self.assertFalse(survival.safe_food(item("Apple", "food", 100, food_safe=1, frozen=frozen)))
                self.assertFalse(survival.safe_drink(item("Canteen", "drink", 100, liquid_safe=1, frozen=frozen)))

    def test_numeric_bleeding_and_harvestability_are_recognized(self):
        s = state(bleeding=1)
        s["inventory"] = [item("BandageDressing")]
        self.assertEqual(self.choose(s)["action"], "treat_other")
        s = state(energy=1000)
        s["nearby"] = [ground("GardenPlot", "garden", 2, harvestable=1)]
        self.assertEqual(self.choose(s)["action"], "harvest_crops")
        s["nearby"][0]["harvestable"] = "1"
        self.assertNotEqual(self.choose(s).get("action"), "harvest_crops")

    def test_just_owned_item_at_feet_is_not_picked_up_again(self):
        s = state(in_hands="Pear")
        s["inventory"] = [item("Pear", "food", 150, in_hands=1)]
        s["nearby"] = [ground("Pear", distance=.1)]
        self.assertNotEqual(self.choose(s).get("action"), "pickup")
        # A distinct copy farther away is still a legitimate supply target.
        s["nearby"] = [ground("Pear", distance=5)]
        self.assertEqual(self.choose(s)["action"], "pickup")

    def test_owned_food_in_cargo_does_not_hide_another_ground_copy(self):
        s = state()
        s["inventory"] = [item("Pear", "food", 150, in_hands=0)]
        s["nearby"] = [ground("Pear", distance=.5)]
        self.assertEqual(self.choose(s)["action"], "pickup")

    def test_far_loot_uses_observed_target_before_pickup(self):
        s = state()
        s["nearby"] = [ground("Apple", distance=25)]
        result = self.choose(s)
        self.assertEqual((result["action"], result["goal"]), ("move_to", "loot_route"))
        self.assertEqual(result["params"], {"x": 1000, "z": 1021})
        self.assertEqual(result["loot_target"]["z"], 1025)

    def test_invalid_observed_target_does_not_become_map_origin(self):
        s = state()
        s["nearby"] = [ground("Apple", distance=25, x="unknown")]
        self.assertNotEqual(self.choose(s).get("action"), "move_to")

    def test_legacy_pickup_success_reward_does_not_bias_new_loot(self):
        s = state()
        s["nearby"] = [ground("Pear", distance=2), ground("Apple", distance=5)]
        memory = survival._new_memory()
        key = survival._key(survival._plan("pickup", "loot", "", text="Apple"), s)
        memory["stats"][key] = {"mean_reward": 1e9, "verified": 40, "attempts": 40}
        self.assertEqual(self.choose(s, memory)["params"]["text"], "Pear")
        self.assertEqual(memory["stats"][key]["verified"], 40)

    def test_radius_and_step_budget_start_return_to_real_anchor(self):
        for distance, steps in ((270, 3), (100, 6)):
            with self.subTest(distance=distance, steps=steps):
                s = state(pos_z=1000 + distance)
                memory = survival._new_memory()
                memory.update(anchor=[1000, 1000], search_steps=steps)
                result = self.choose(s, memory, explore=True)
                self.assertEqual(result["goal"], "return")
                self.assertEqual(result["params"]["x"], 1000)
                self.assertLess(result["params"]["z"], s["npc"]["pos_z"])
                self.assertLessEqual(s["npc"]["pos_z"] - result["params"]["z"], 60)

    def test_return_phase_continues_inside_radius(self):
        s = state(pos_z=1040)
        memory = survival._new_memory()
        memory.update(anchor=[1000, 1000], search_steps=3, search_returning=True)
        self.assertEqual(self.choose(s, memory, explore=True)["goal"], "return")

    def test_visible_water_beats_uninformed_compass_search(self):
        s = state()
        s["nearby"] = [ground("Well_Pump", "water", 30, x=1030, z=1000)]
        result = self.choose(s, explore=True)
        self.assertEqual(result["goal"], "explore_site")
        self.assertEqual(result["params"], {"x": 1026, "z": 1000})

    def test_weather_uses_only_reported_shelter_or_entrance(self):
        s = state(wet=1)
        s["nearby"] = [ground("ObservedShelter", "shelter", 25)]
        self.assertEqual(self.choose(s, explore=True)["goal"], "shelter_route")
        s["nearby"] = [ground("Land_Shed", "building", 25)]
        self.assertNotEqual(self.choose(s, explore=True).get("goal"), "shelter_route")
        s["nearby"][0].update(entrance_x=1010, entrance_z=1020)
        self.assertEqual(self.choose(s, explore=True)["goal"], "shelter_route")

    def test_blocked_return_never_resumes_outward_random_search(self):
        s = state(pos_z=1260)
        memory = survival._new_memory()
        memory["anchor"] = [1000, 1000]
        first = self.choose(s, memory, explore=True)
        memory["stats"][survival._key(first, s)] = {"retry_after": 1100}
        blocked = self.choose(s, memory, explore=True)
        self.assertTrue(blocked["needs_llm"])
        self.assertNotIn("action", blocked)

    def test_full_stomach_prevents_food_and_water(self):
        s = state(energy=100, water=100, stomach_volume=1100)
        s["inventory"] = [item("Apple", "food", 100), item("SodaCan_Pipsi", "drink", 100)]
        s["nearby"] = [ground("Well_Pump", "water", 2)]
        self.assertNotIn(self.choose(s).get("action"), ("eat", "drink", "drink_well"))

    def test_passive_animals_are_not_combat_targets(self):
        s = state()
        s["nearby"] = [ground("Animal_CervusElaphus", "animal", 2), ground("Apple")]
        self.assertEqual(self.choose(s)["action"], "pickup")
        s["nearby"] = [ground("Animal_CanisLupus", "animal", 2)]
        self.assertEqual(self.choose(s)["action"], "flee")

    def test_nearby_human_is_never_attacked(self):
        s = state()
        s["inventory"] = [item("M4A1", "firearm", 30)]
        s["nearby"] = [ground("SurvivorM", "player", 2)]
        result = self.choose(s, explore=True)
        self.assertTrue(result["needs_llm"])
        self.assertNotIn(result.get("action"), ("engage", "hunt"))

    def test_warm_and_cool_clothing_choices(self):
        s = state(heat_comfort=-.7)
        s["inventory"] = [item("TShirt", "clothing", slot="Body", worn=True, warmth=.1, cargo_size=12),
                          item("DownJacket", "clothing", slot="Body", warmth=.9, cargo_size=12)]
        self.assertEqual(self.choose(s)["params"]["text"], "DownJacket")
        s["npc"]["heat_comfort"] = .9
        s["inventory"][0]["worn"] = False
        s["inventory"][1]["worn"] = True
        self.assertEqual(self.choose(s)["params"]["text"], "TShirt")

    def test_fire_escape_beats_loot(self):
        s = state()
        s["nearby"] = [ground("Fireplace", "fire_burning", 1), ground("M4A1")]
        decision = self.choose(s)
        self.assertEqual(decision["goal"], "heat_safety")
        self.assertLess(decision["params"]["z"], 1000)

    def test_disease_uses_exact_single_medication_primitive(self):
        s = state(disease={"agents": {"salmonella": 300}, "sick": True})
        s["inventory"] = [item("CharcoalTablets", "medicine", 8)]
        decision = self.choose(s)
        self.assertEqual((decision["action"], decision["params"]["text"]), ("take_medicine", "CharcoalTablets"))
        s["bridge_version"] = "0.8.0"
        self.assertNotEqual(self.choose(s).get("action"), "take_medicine")

    def test_garden_requires_observed_ripe_plants(self):
        s = state(energy=1000)
        s["nearby"] = [ground("GardenPlot", "garden", 2, harvestable=True)]
        self.assertEqual(self.choose(s)["action"], "harvest_crops")
        s["nearby"][0]["harvestable"] = False
        self.assertNotEqual(self.choose(s).get("action"), "harvest_crops")

    def test_hunt_requires_knife_and_viable_weapon(self):
        s = state(energy=1000)
        s["nearby"] = [ground("Animal_CervusElaphus", "animal", 60)]
        self.assertNotEqual(self.choose(s).get("action"), "hunt")
        s["inventory"] = [item("HuntingKnife"), item("M4A1", "firearm", 10, in_hands=True)]
        s["npc"]["in_hands"] = "M4A1"
        self.assertEqual(self.choose(s)["action"], "hunt")

    def test_learning_never_overrules_critical_priorities(self):
        s = state(water=200)
        s["nearby"] = [ground("Well_Pump", "water", 2), ground("M4A1")]
        memory = survival._new_memory()
        plan = survival._plan("pickup", "loot", "", text="M4A1")
        memory["stats"][survival._key(plan, s)] = {"mean_reward": 1e9}
        self.assertEqual(self.choose(s, memory)["action"], "drink_well")

    def test_cooked_meat_does_not_loop_through_cook(self):
        self.assertFalse(tactics._has_raw_food({"inventory": [item("DeerSteakMeat", food_stage=2)]}))
        self.assertTrue(tactics._has_raw_food({"inventory": [item("DeerSteakMeat", food_stage=1)]}))

    def test_inventory_cleanup_never_drops_ambiguous_or_loaded_container(self):
        s = state()
        memory = survival._new_memory()
        memory["inventory_full"] = True
        s["inventory"] = [item("HuntingKnife", health=0), item("HuntingKnife", in_hands=True)]
        self.assertNotEqual(self.choose(s, memory).get("action"), "drop")
        s["inventory"] = [item("CookingPot", health=0)]
        self.assertNotEqual(self.choose(s, memory).get("action"), "drop")
        s["inventory"] = [item("M4A1", "firearm", health=0), item("Mag_STANAG_30Rnd", "ammo", parent="M4A1")]
        self.assertNotEqual(self.choose(s, memory).get("action"), "drop")
        s["inventory"] = [item("HuntingKnife", health=0)]
        self.assertEqual(self.choose(s, memory).get("action"), "drop")


class SurvivalControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "memory.json")

    def tick(self, bridge, now=1000, explore=False):
        return survival.survival_tick(bridge, "Viktor", self.path, explore, now)

    def bridge(self, snapshot):
        return FakeBridge(snapshot, self.tmp.name)

    def test_pending_is_short_no_duplicate_and_one_action(self):
        s = state(energy=200)
        s["inventory"] = [item("Apple", "food", 100)]
        bridge = self.bridge(s)
        self.assertEqual(self.tick(bridge)["status"], "started")
        self.assertEqual(self.tick(bridge)["status"], "stale")
        bridge.advance("running")
        self.assertEqual(self.tick(bridge, 1005)["status"], "running")
        self.assertEqual(len(bridge.sent), 1)

    def test_learning_requires_changed_outcome_not_done_string(self):
        s = state(energy=200)
        s["inventory"] = [item("Apple", "food", 100)]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance()
        result = self.tick(bridge, 1005)
        self.assertFalse(result["learning"]["verified"])
        self.assertEqual(survival.memory_summary(self.path)["verified_outcomes"], 0)
        self.assertEqual(len(bridge.sent), 1)

    def test_hand_swap_is_not_success_and_dropped_fruit_is_not_chased(self):
        s = state()
        s["inventory"] = [item("Apple", "food", 125)]
        s["nearby"] = [ground("Pear", distance=2)]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance()
        bridge.snapshot["inventory"] = [item("Pear", "food", 150)]
        bridge.snapshot["nearby"] = [ground("Apple", distance=.1)]
        result = self.tick(bridge, 1005)
        self.assertFalse(result["learning"]["verified"])
        self.assertIn("Apple", result["learning"]["evidence"])
        self.assertEqual(len(bridge.sent), 1)
        memory = survival.load_memory(self.path)
        self.assertEqual({entry["classname"] for entry in memory["loot_targets"].values()}, {"Apple", "Pear"})
        # Old positive totals remain recorded, but the new reward is negative.
        self.assertLess(next(iter(memory["stats"].values()))["mean_reward"], 0)

    def test_failed_loot_is_blocked_across_player_cell_and_goal_changes(self):
        s = state(pos_x=1049)
        s["nearby"] = [ground("Pear", distance=5, x=1054, z=1000)]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance("failed", pos_x=1051, energy=200)
        bridge.snapshot["nearby"][0]["distance"] = 3
        self.tick(bridge, 1005)
        self.assertEqual(len(bridge.sent), 1)
        # A new process preserves the suppression after the old 60-s backoff.
        fresh = self.bridge(copy.deepcopy(bridge.snapshot))
        fresh.snapshot["seq"] += 1
        fresh.snapshot["uptime"] += 80
        self.tick(fresh, 1085)
        self.assertEqual(fresh.sent, [])

    def test_failed_loot_route_moves_on_to_other_observed_loot(self):
        s = state()
        s["nearby"] = [ground("M4A1", distance=25), ground("Apple", distance=5)]
        bridge = self.bridge(s)
        self.tick(bridge)
        self.assertEqual(bridge.sent[-1][0], "move_to")
        bridge.advance("failed")
        self.tick(bridge, 1005)
        self.assertEqual(bridge.sent[-1], ("pickup", {"text": "Apple"}))

    def test_confirmed_pickup_cannot_retarget_same_location_immediately(self):
        s = state()
        s["nearby"] = [ground("Apple", distance=2)]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance()
        bridge.snapshot["inventory"] = [item("Apple", "food", 125)]
        bridge.snapshot["nearby"] = []
        self.assertTrue(self.tick(bridge, 1005)["learning"]["verified"])
        bridge.advance()
        bridge.snapshot["inventory"] = []
        bridge.snapshot["nearby"] = [ground("Apple", distance=2)]
        self.tick(bridge, 1010)
        self.assertEqual(len(bridge.sent), 1)

    def test_second_fruit_after_consumption_is_not_blocked_for_ten_minutes(self):
        s = state()
        s["nearby"] = [ground("Apple", distance=2)]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance()
        bridge.snapshot["inventory"] = [item("Apple", "food", 125)]
        bridge.snapshot["nearby"] = []
        self.assertTrue(self.tick(bridge, 1005)["learning"]["verified"])
        bridge.advance(energy=500, in_hands="")
        bridge.snapshot["inventory"] = []
        bridge.snapshot["nearby"] = [ground("Apple", distance=2)]
        self.tick(bridge, 1040)
        self.assertEqual(len(bridge.sent), 2)
        self.assertEqual(bridge.sent[-1], ("pickup", {"text": "Apple"}))

    def test_old_pickup_reward_is_reset_without_deleting_history(self):
        s = state()
        s["nearby"] = [ground("Pear", distance=2)]
        key = survival._key(survival._plan("pickup", "loot", "", text="Pear"), s)
        memory = survival._new_memory()
        memory["stats"][key] = {"attempts": 40, "verified": 40, "failures": 0,
                                "streak": 0, "mean_reward": 1}
        memory["episodes"] = [{"action": "pickup", "verified": True, "legacy": True}]
        survival._save(self.path, memory)
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance()
        self.tick(bridge, 1005)
        updated = survival.load_memory(self.path)
        self.assertEqual(updated["stats"][key]["attempts"], 41)
        self.assertEqual(updated["stats"][key]["verified"], 40)
        self.assertEqual(updated["stats"][key]["reward_samples"], 1)
        self.assertLess(updated["stats"][key]["mean_reward"], 0)
        self.assertTrue(updated["episodes"][0]["legacy"])

    def test_search_returns_and_rest_then_changes_search_sector(self):
        bridge = self.bridge(state())
        result = self.tick(bridge, explore=True)
        goals, now = [], 1000
        while result["status"] == "started" and len(goals) < 20:
            goals.append(result["goal"])
            params = bridge.sent[-1][1]
            self.assertLessEqual(math.hypot(params["x"] - 1000, params["z"] - 1000), 280)
            bridge.advance(pos_x=params["x"], pos_z=params["z"])
            now += 6
            result = self.tick(bridge, now, explore=True)
        self.assertIn("return", goals)
        self.assertLess(len(goals), 20)
        self.assertEqual(result["status"], "idle")
        self.assertLessEqual(math.dist(survival._pos(bridge.snapshot["npc"]), (1000, 1000)), 5)
        memory = survival.load_memory(self.path)
        self.assertFalse(memory["search_returning"])
        self.assertEqual(memory["search_steps"], 0)
        bridge.advance()
        self.tick(bridge, now + 46, explore=True)
        self.assertGreater(bridge.sent[-1][1]["x"], 1000)

    def test_numeric_live_flags_and_invalid_flags_fail_closed(self):
        bridge = self.bridge(state(alive=1, bleeding=1))
        bridge.snapshot["inventory"] = [item("BandageDressing", quantity=4)]
        self.assertEqual(self.tick(bridge)["action"], "treat_other")
        bridge.advance(bleeding=0)
        self.assertTrue(self.tick(bridge, 1005)["learning"]["verified"])
        bridge.advance(alive=0)
        self.assertEqual(self.tick(bridge, 1010, explore=True)["status"], "inactive")
        bridge.advance(alive=1, unconscious="false")
        self.assertEqual(self.tick(bridge, 1015, explore=True)["status"], "unavailable")
        self.assertEqual(len(bridge.sent), 1)

    def test_measured_improvement_survives_model_process_restart(self):
        s = state(energy=200)
        s["inventory"] = [item("Apple", "food", 100)]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance(energy=250, stomach_volume=100)
        result = self.tick(bridge, 1005)
        self.assertTrue(result["learning"]["verified"])
        summary = survival.memory_summary(self.path)
        self.assertEqual(summary["verified_outcomes"], 1)
        self.assertEqual(summary["recent_outcomes"][0]["delta"]["energy"], 50)
        fresh_process = self.bridge(copy.deepcopy(bridge.snapshot))
        fresh_process.snapshot["seq"] += 1
        self.tick(fresh_process, 1010)
        self.assertEqual(fresh_process.sent, [])  # digestion cooldown persists

    def test_failed_loot_target_changes_instead_of_repeating(self):
        s = state()
        s["nearby"] = [ground("M4A1"), ground("Apple")]
        bridge = self.bridge(s)
        self.tick(bridge)
        self.assertEqual(bridge.sent[0][1]["text"], "M4A1")
        bridge.advance("failed")
        self.tick(bridge, 1005)
        self.assertEqual(bridge.sent[1][1]["text"], "Apple")

    def test_follow_vehicle_unconscious_external_running_are_protected(self):
        for field in ("following", "in_vehicle", "unconscious"):
            with self.subTest(field=field):
                bridge = self.bridge(state(**{field: True}))
                bridge.snapshot["seq"] += len(field)
                self.tick(bridge, explore=True)
                self.assertEqual(bridge.sent, [])
        bridge = self.bridge(state())
        bridge.snapshot["command"] = {"id": "human", "action": "move_to", "status": "running"}
        self.tick(bridge, explore=True)
        self.assertEqual(bridge.sent, [])

    def test_explicit_stop_remains_stopped(self):
        bridge = self.bridge(state())
        bridge.snapshot["command"] = {"id": "human", "action": "stop", "status": "done"}
        self.tick(bridge, explore=True)
        self.assertEqual(bridge.sent, [])

    def test_travel_lease_protects_gap_between_segments(self):
        bridge = self.bridge(state())
        with open(os.path.join(bridge.dir, "travel_active_offline.json"), "w") as stream:
            json.dump({"pid": os.getpid()}, stream)
        self.assertEqual(self.tick(bridge, explore=True)["status"], "busy")
        self.assertEqual(bridge.sent, [])

    def test_bleeding_interrupts_own_loot_only(self):
        s = state()
        s["inventory"] = [item("BandageDressing")]
        s["nearby"] = [ground("M4A1")]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance("running", bleeding=True)
        self.tick(bridge, 1005)
        self.assertEqual(bridge.sent[-1][0], "treat_other")

    def test_external_action_supersedes_own_pending_without_safety_takeover(self):
        s = state()
        s["inventory"] = [item("BandageDressing")]
        s["nearby"] = [ground("M4A1")]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance("running", bleeding=True)
        bridge.snapshot["command"] = {"id": "human_move", "action": "move_to", "status": "running"}
        result = self.tick(bridge, 1005)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(len(bridge.sent), 1)

    def test_multiple_bleeding_sources_are_treated_without_failure_backoff(self):
        s = state(bleeding=True)
        s["inventory"] = [item("Rag", quantity=6)]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance(bleeding=True)
        bridge.snapshot["inventory"][0]["quantity"] = 5
        result = self.tick(bridge, 1005)
        self.assertIn("weitere Blutung", result["learning"]["evidence"])
        bridge.advance()
        self.tick(bridge, 1010)
        self.assertEqual(bridge.sent[-1][0], "treat_other")
        self.assertEqual(len(bridge.sent), 2)

    def test_old_state_file_prevents_actions(self):
        bridge = self.bridge(state())
        bridge.state_file = os.path.join(self.tmp.name, "state.json")
        with open(bridge.state_file, "w") as stream:
            json.dump(bridge.snapshot, stream)
        os.utime(bridge.state_file, (10, 10))
        self.assertEqual(self.tick(bridge, explore=True)["status"], "stale")
        self.assertEqual(bridge.sent, [])

    def test_medication_has_persistent_cooldown_and_no_false_cure_claim(self):
        s = state(disease={"agents": {"salmonella": 300}, "sick": True})
        s["inventory"] = [item("CharcoalTablets", "medicine", 8)]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.advance()
        bridge.snapshot["inventory"][0]["quantity"] = 7
        result = self.tick(bridge, 1005)
        self.assertTrue(result["learning"]["verified"])
        self.assertIn("NICHT", result["learning"]["evidence"])
        bridge.advance()
        self.tick(bridge, 1010)
        self.assertEqual(len(bridge.sent), 1)

    def test_server_restart_does_not_learn_false_failure(self):
        s = state()
        s["nearby"] = [ground("M4A1")]
        bridge = self.bridge(s)
        self.tick(bridge)
        bridge.snapshot = state()
        bridge.snapshot["seq"], bridge.snapshot["uptime"] = 0, 1
        self.tick(bridge, 1005)
        self.assertEqual(survival.memory_summary(self.path)["attempts"], 0)

    def test_mailbox_and_memory_lock_prevent_concurrent_send(self):
        bridge = self.bridge(state())
        with survival._memory_lock(self.path):
            self.assertEqual(self.tick(bridge, explore=True)["status"], "busy")
        bridge.cmd_file = os.path.join(self.tmp.name, "commands.json")
        with open(bridge.cmd_file, "w") as stream:
            stream.write("{}")
        self.assertEqual(self.tick(bridge, explore=True)["status"], "busy")
        self.assertEqual(bridge.sent, [])

    def test_search_has_budget_not_endless_wandering(self):
        s = state()
        memory = survival._new_memory()
        memory["search_steps"] = 6
        decision = survival.choose_action(s, memory, 1000, "Viktor", True)
        self.assertTrue(decision["needs_llm"])
        self.assertNotIn("action", decision)


if __name__ == "__main__":
    unittest.main()
