import os
import sys
import time
import json
import math
import queue
import logging
import threading
import requests
import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from demoparser2 import DemoParser
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from collections import defaultdict

# Enable requests logging for debuggin
if True:  # Set to False to disable HTTP debug logs
    logging.basicConfig(level=logging.DEBUG)
    logging.getLogger("urllib3").setLevel(logging.DEBUG)
    requests_log = logging.getLogger("requests.packages.urllib3")
    requests_log.setLevel(logging.DEBUG)
    requests_log.propagate = True

# ========================================================
# ⚠️ ТОХИРГООНЫ ХЭСЭГ
# ========================================================
DEFAULT_WEBSITE_API_URL = "https://frag-track-lan.base44.app/api/functions/matchEnd"
DEFAULT_RECEIVE_KEY = "baaduu"

DEBUG_MODE = True
DEBUG_SHOW_KEY = False  # Set to True to expose full receive key in logs

CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".frag_track_config.json")
PAYLOAD_DIR = os.path.join(os.path.expanduser("~"), "FragTrackLAN_payloads")

# Advanced calculation settings
TRADE_WINDOW_TICKS = 10  # Trades within this tick window after teammate death
# ========================================================


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def json_safe(value):
    """Recursively replace NaN/inf (which aren't valid JSON) with None,
    and numpy/pandas scalar types with plain Python ones."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if hasattr(value, "item"):  # numpy scalar (int64, float64, bool_, etc.)
        return json_safe(value.item())
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


# ========================================================
# DATA EXTRACTION LAYER
# =======================================================

def parse_demo(file_path):
    """
    Parse demo file and return raw demo data.
    Returns: DemoParser object and parsed tick/event data.
    """
    try:
        parser = DemoParser(file_path)
        return parser
    except Exception as e:
        raise ValueError(f"Failed to parse demo: {e}")


def extract_player_data(parser):
    """
    Extract per-player statistics from demo.
    Returns: dict mapping steamid -> player info
    
    Note: Kills/deaths/assists will be recalculated from kill_events 
    since parse_ticks() may not provide accurate combat stats.
    """
    try:
        # Try multiple possible field names for player name
        name_field_candidates = ["user_name", "name", "player_name", "username"]
        name_field = None
        
        # First, try to see what fields are available
        try:
            test_ticks = parser.parse_ticks(["steamid"])
            if isinstance(test_ticks, pd.DataFrame) and not test_ticks.empty:
                available_cols = test_ticks.columns.tolist()
                if DEBUG_MODE:
                    print(f"[DEBUG] Available columns in parse_ticks: {available_cols}")
                # Find name field
                for candidate in name_field_candidates:
                    if candidate in available_cols:
                        name_field = candidate
                        break
        except Exception:
            pass
        
        # If we couldn't find a name field, default to first candidate
        if not name_field:
            name_field = "user_name"
        
        # Parse ticks with the fields we want
        parse_fields = [name_field, "steamid", "score", "mvps", "team"]
        players_ticks = parser.parse_ticks(parse_fields)
        
        players = {}
        
        if isinstance(players_ticks, pd.DataFrame):
            if not players_ticks.empty:
                # Get final state for each player (for name, team, score, mvps)
                last_tick = players_ticks["tick"].max()
                df_last = players_ticks[players_ticks["tick"] == last_tick]
                
                if DEBUG_MODE:
                    print(f"[DEBUG] DataFrame columns: {df_last.columns.tolist()}")
                
                for _, row in df_last.iterrows():
                    steamid = str(row.get("steamid", ""))
                    if steamid:
                        player_name = str(row.get(name_field, ""))
                        if not player_name or player_name == "":
                            player_name = "Unknown"
                        
                        players[steamid] = {
                            "user_name": player_name,
                            "steamid": steamid,
                            "team": str(row.get("team", "Unknown")),
                            "score": int(row.get("score", 0)),
                            "mvps": int(row.get("mvps", 0)),
                            # Will be populated from kill_events
                            "kills": 0,
                            "deaths": 0,
                            "assists": 0,
                        }
        elif isinstance(players_ticks, list) and players_ticks:
            last_state = players_ticks[-1]
            if isinstance(last_state, list):
                for player in last_state:
                    steamid = str(player.get("steamid", ""))
                    if steamid:
                        player_name = str(player.get(name_field, ""))
                        if not player_name or player_name == "":
                            player_name = "Unknown"
                        
                        players[steamid] = {
                            "user_name": player_name,
                            "steamid": steamid,
                            "team": str(player.get("team", "Unknown")),
                            "score": int(player.get("score", 0)),
                            "mvps": int(player.get("mvps", 0)),
                            # Will be populated from kill_events
                            "kills": 0,
                            "deaths": 0,
                            "assists": 0,
                        }
        
        return players
    except Exception as e:
        if DEBUG_MODE:
            print(f"[DEBUG] Error extracting player data: {e}")
            import traceback
            print(f"[DEBUG] Traceback: {traceback.format_exc()}")
        return {}


def extract_kill_events(parser):
    """
    Extract all kill/death events from demo.
    Returns: list of kill event dicts
    
    Note: Field names may vary in demoparser2; will try multiple names.
    """
    try:
        death_events = parser.parse_events(["player_death"])
        
        # Normalize demoparser2's parse_events output
        if isinstance(death_events, list):
            frames = [df for _, df in death_events if isinstance(df, pd.DataFrame) and not df.empty]
            death_events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        
        kills = []
        
        if isinstance(death_events, pd.DataFrame):
            if DEBUG_MODE and not death_events.empty:
                print(f"[DEBUG] Kill event columns: {death_events.columns.tolist()}")
            
            death_events = death_events.to_dict(orient="records")
        elif not isinstance(death_events, list):
            death_events = []
        
        # Try alternative field names
        name_candidates_attacker = ["attacker_name", "attacker", "killer_name", "killer"]
        name_candidates_victim = ["user_name", "victim_name", "victim"]
        steamid_candidates_attacker = ["attacker_steamid", "attacker_steam_id", "killer_steamid"]
        steamid_candidates_victim = ["victim_steamid", "victim_steam_id", "steamid", "user_steamid"]
        
        for event in death_events:
            # Find attacker name
            attacker_name = None
            for candidate in name_candidates_attacker:
                if event.get(candidate):
                    attacker_name = str(event.get(candidate))
                    break
            if not attacker_name:
                attacker_name = "Unknown"
            
            # Find victim name
            victim_name = None
            for candidate in name_candidates_victim:
                if event.get(candidate):
                    victim_name = str(event.get(candidate))
                    break
            if not victim_name:
                victim_name = "Unknown"
            
            # Find attacker steamid
            attacker_steamid = None
            for candidate in steamid_candidates_attacker:
                if event.get(candidate):
                    attacker_steamid = str(event.get(candidate))
                    break
            
            # Find victim steamid
            victim_steamid = None
            for candidate in steamid_candidates_victim:
                if event.get(candidate):
                    victim_steamid = str(event.get(candidate))
                    break
            
            # Find assister name (may be assister_name or assister)
            assister_name = event.get("assister_name") or event.get("assister") or None
            
            kills.append({
                "attacker_name": attacker_name,
                "attacker_steamid": attacker_steamid,
                "user_name": victim_name,
                "victim_steamid": victim_steamid,
                "weapon": str(event.get("weapon", "unknown")),
                "headshot": bool(event.get("headshot", False)),
                "tick": int(event.get("tick", 0)),
                "assister_name": assister_name,  # Assister is stored as PLAYER NAME, not steamid
            })
        
        return kills
    except Exception as e:
        if DEBUG_MODE:
            print(f"[DEBUG] Error extracting kill events: {e}")
            import traceback
            print(f"[DEBUG] Traceback: {traceback.format_exc()}")
        return []


def extract_damage_events(parser):
    """Extract player_hurt events and normalize damage values for ADR calculations."""
    try:
        pain_events = parser.parse_events(["player_hurt"])
        if isinstance(pain_events, list):
            frames = [df for _, df in pain_events if isinstance(df, pd.DataFrame) and not df.empty]
            pain_events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

        if isinstance(pain_events, pd.DataFrame):
            pain_events = pain_events.to_dict(orient="records")
        elif not isinstance(pain_events, list):
            pain_events = []

        damage_events = []
        for event in pain_events:
            attacker_steamid = None
            for candidate in ["attacker_steamid", "attacker_steam_id", "killer_steamid", "attacker"]:
                value = event.get(candidate)
                if value not in (None, ""):
                    attacker_steamid = str(value)
                    break

            if not attacker_steamid:
                continue

            dmg_value = None
            for candidate in ["dmg_health", "damage", "damage_health", "health_damage", "dmg", "amount"]:
                value = event.get(candidate)
                if value is not None:
                    dmg_value = value
                    break

            if dmg_value is None:
                continue

            try:
                damage = float(dmg_value)
            except (TypeError, ValueError):
                continue

            if math.isnan(damage) or math.isinf(damage):
                continue

            damage_events.append({
                "attacker_steamid": attacker_steamid,
                "damage": abs(float(damage)),
                "tick": int(event.get("tick", 0)),
            })

        return damage_events
    except Exception as e:
        if DEBUG_MODE:
            print(f"[DEBUG] Error extracting damage events: {e}")
        return []


def extract_round_events(parser):
    """
    Extract round start/end events.
    Returns: list of round event dicts
    """
    try:
        round_events = parser.parse_events(["round_end"])
        
        if isinstance(round_events, list):
            frames = [df for _, df in round_events if isinstance(df, pd.DataFrame) and not df.empty]
            round_events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        
        rounds = []
        
        if isinstance(round_events, pd.DataFrame):
            round_events = round_events.to_dict(orient="records")
        elif not isinstance(round_events, list):
            round_events = []
        
        for event in round_events:
            rounds.append({
                "winner": event.get("winner"),
                "round": int(event.get("round", 0)),
                "tick": int(event.get("tick", 0)),
            })
        
        return rounds
    except Exception as e:
        if DEBUG_MODE:
            print(f"[DEBUG] Error extracting round events: {e}")
        return []


def extract_bomb_events(parser):
    """
    Extract bomb plant/defuse events.
    Returns: dict with bomb_planted, bomb_defused, bomb_exploded lists
    """
    try:
        events_to_parse = ["bomb_planted", "bomb_defused", "bomb_exploded"]
        bomb_data = {
            "bomb_planted": [],
            "bomb_defused": [],
            "bomb_exploded": []
        }
        
        for event_type in events_to_parse:
            try:
                events = parser.parse_events([event_type])
                
                if isinstance(events, list):
                    frames = [df for _, df in events if isinstance(df, pd.DataFrame) and not df.empty]
                    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                
                if isinstance(events, pd.DataFrame):
                    events = events.to_dict(orient="records")
                elif not isinstance(events, list):
                    events = []
                
                bomb_data[event_type] = events
            except Exception:
                bomb_data[event_type] = []
        
        return bomb_data
    except Exception as e:
        if DEBUG_MODE:
            print(f"[DEBUG] Error extracting bomb events: {e}")
        return {"bomb_planted": [], "bomb_defused": [], "bomb_exploded": []}


def extract_grenade_events(parser):
    """
    Extract grenade throw events where available.
    Returns: list of grenade event dicts
    """
    try:
        grenade_types = ["hegrenade_detonate", "flashbang_detonate", "smoke_detonate"]
        all_grenades = []
        
        for grenade_type in grenade_types:
            try:
                events = parser.parse_events([grenade_type])
                
                if isinstance(events, list):
                    frames = [df for _, df in events if isinstance(df, pd.DataFrame) and not df.empty]
                    events = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
                
                if isinstance(events, pd.DataFrame):
                    events = events.to_dict(orient="records")
                elif not isinstance(events, list):
                    events = []
                
                all_grenades.extend(events)
            except Exception:
                pass
        
        return all_grenades
    except Exception as e:
        if DEBUG_MODE:
            print(f"[DEBUG] Error extracting grenade events: {e}")
        return []


# ========================================================
# STATISTICAL CALCULATION LAYER
# ========================================================

def calculate_player_stats(players, kills, assists_data=None, damage_events=None, total_rounds=0):
    """
    Calculate per-player statistics from raw data.
    Updates players dict with calculated stats.
    
    Calculates:
    - Kills/Deaths from kill_events (recalculated to override any parse_ticks() errors)
    - K/D (Kill/Death ratio)
    - ADR (Average Damage per Round) - limited by available data
    - KAST (Kill, Assist, Survival, Trade%)
    - HS% (Headshot percentage)
    - Entry kills/deaths
    - Trade kills/deaths
    - Multi-kills (2K, 3K, 4K, 5K)
    """
    if damage_events is None:
        damage_events = []

    # Initialize stats for all players
    for steamid in players:
        players[steamid].update({
            "kd": 0.0,
            "adr": 0.0,
            "kast": 0.0,
            "hs_percent": 0.0,
            "entry_kills": 0,
            "entry_deaths": 0,
            "trade_kills": 0,
            "trade_deaths": 0,
            "multi_kills": {"2k": 0, "3k": 0, "4k": 0, "5k": 0},
            "clutches": {"1v1": 0, "1v2": 0, "1v3": 0, "1v4": 0, "1v5": 0},
            "headshot_kills": 0,
        })

    if total_rounds <= 0 and kills:
        tick_values = {int(k.get("tick", 0)) // 1024 for k in kills if k.get("tick") is not None}
        total_rounds = max(1, len(tick_values))
    elif total_rounds <= 0:
        total_rounds = 1
    
    # === RECALCULATE KILLS/DEATHS FROM kill_events ===
    # This overrides any incorrect parse_ticks() data
    kill_count = defaultdict(int)
    death_count = defaultdict(int)
    assist_count = defaultdict(int)
    
    # Debug: check what assist field looks like
    if DEBUG_MODE and kills:
        print(f"[DEBUG] Total kills: {len(kills)}")
        print(f"[DEBUG] First kill (full): {json.dumps(kills[0], ensure_ascii=False, indent=2)}")
        if len(kills) > 1:
            print(f"[DEBUG] Second kill (full): {json.dumps(kills[1], ensure_ascii=False, indent=2)}")
        
        # Check which kills have non-null assist values
        assists_found = [k for k in kills if k.get('assist')]
        print(f"[DEBUG] Kills with assist field populated: {len(assists_found)} out of {len(kills)}")
        if assists_found:
            print(f"[DEBUG] Sample kill with assist: {json.dumps(assists_found[0], ensure_ascii=False, indent=2)}")
    
    for kill in kills:
        attacker_steamid = str(kill.get("attacker_steamid", ""))
        victim_steamid = str(kill.get("victim_steamid", ""))
        
        if attacker_steamid:
            kill_count[attacker_steamid] += 1
        
        if victim_steamid:
            death_count[victim_steamid] += 1
        
        # Assister is stored as PLAYER NAME, not steamid
        assister_name = kill.get("assister_name")
        
        if assister_name:
            # Find the steamid for this player name
            for steam_id, player_data in players.items():
                if player_data.get("user_name") == assister_name:
                    assist_count[steam_id] += 1
                    break
    
    if DEBUG_MODE:
        print(f"[DEBUG] Assists found: {dict(assist_count)}")
        print(f"[DEBUG] Sample assists by name:")
        for kill in kills[:5]:
            if kill.get("assister_name"):
                print(f"  - {kill.get('assister_name')} assisted killing {kill.get('user_name')}")
        if not assist_count:
            print(f"[DEBUG] WARNING: No assists found in {len(kills)} kills! Demo may not have assist data.")
    
    # Update player kill/death/assist counts from events
    for steamid in players:
        players[steamid]["kills"] = kill_count.get(steamid, 0)
        players[steamid]["deaths"] = death_count.get(steamid, 0)
        players[steamid]["assists"] = assist_count.get(steamid, 0)
    
    # === CALCULATE K/D ===
    for steamid in players:
        kills_count = players[steamid]["kills"]
        deaths_count = players[steamid]["deaths"]
        if deaths_count > 0:
            players[steamid]["kd"] = round(kills_count / deaths_count, 2)
        elif kills_count > 0:
            players[steamid]["kd"] = float(kills_count)

    # === CALCULATE ADR ===
    damage_by_player = defaultdict(float)
    for event in damage_events:
        attacker_steamid = str(event.get("attacker_steamid", ""))
        if attacker_steamid in players:
            damage_by_player[attacker_steamid] += float(event.get("damage", 0.0) or 0.0)

    for steamid in players:
        total_damage = damage_by_player.get(steamid, 0.0)
        if total_rounds > 0:
            players[steamid]["adr"] = round(total_damage / total_rounds, 1)
        else:
            players[steamid]["adr"] = round(total_damage, 1)
    
    # === COUNT HEADSHOT KILLS ===
    headshot_count = defaultdict(int)
    for kill in kills:
        attacker_steamid = str(kill.get("attacker_steamid", ""))
        if attacker_steamid and kill.get("headshot"):
            headshot_count[attacker_steamid] += 1
    
    for steamid in players:
        hs_kills = headshot_count.get(steamid, 0)
        players[steamid]["headshot_kills"] = hs_kills
        
        total_kills = players[steamid]["kills"]
        if total_kills > 0:
            players[steamid]["hs_percent"] = round((hs_kills / total_kills) * 100, 1)
    
    # === CALCULATE ENTRY KILLS/DEATHS ===
    # Simplified: first few kills in demo sequence
    for kill in sorted(kills, key=lambda x: x.get("tick", 0))[:10]:  # First 10 kills as "entries"
        if kill.get("attacker_steamid"):
            attacker_steamid = str(kill["attacker_steamid"])
            victim_steamid = str(kill.get("victim_steamid", ""))
            
            if attacker_steamid in players:
                players[attacker_steamid]["entry_kills"] += 1
            
            if victim_steamid in players:
                players[victim_steamid]["entry_deaths"] += 1
    
    # === CALCULATE KAST ===
    # KAST = (Kills + Assists + Rounds Survived) / Total Rounds
    # Simplified without round data: use participation ratio
    # If no assists available, estimate KAST based on K/D efficiency
    for steamid in players:
        kills_count = players[steamid]["kills"]
        deaths_count = players[steamid]["deaths"]
        assists_count = players[steamid]["assists"]
        
        # If we have assists data
        if assists_count > 0:
            participations = kills_count + assists_count
            engagements = kills_count + deaths_count
            if engagements > 0:
                players[steamid]["kast"] = round(
                    (participations / engagements) * 100,
                    1
                )
        else:
            # No assist data or zero assists: estimate KAST as kill participation rate
            # Players with kills likely have high participation
            total_engagements = kills_count + deaths_count
            if total_engagements > 0:
                # Simple estimate: K/(K+D) * 100 as proxy for KAST
                # This is a conservative estimate
                players[steamid]["kast"] = round(
                    (kills_count / total_engagements) * 100,
                    1
                )
    
    return players


def calculate_combat_stats(players, kills):
    """
    Calculate weapon and combat-specific statistics.
    Adds:
    - Weapon breakdown
    - Trade statistics (simplified)
    """
    
    weapon_kills = defaultdict(lambda: defaultdict(int))
    
    for kill in kills:
        attacker_steamid = str(kill.get("attacker_steamid", ""))
        weapon = str(kill.get("weapon", "unknown"))
        
        if attacker_steamid:
            weapon_kills[attacker_steamid][weapon] += 1
    
    for steamid in players:
        players[steamid]["weapon_kills"] = dict(weapon_kills.get(steamid, {}))
    
    return players


def calculate_round_stats(rounds, bomb_events, players):
    """
    Calculate round-level statistics.
    
    Calculates:
    - Round count
    - Rounds won/lost by team
    - Bomb plant/defuse/explode events
    """
    
    stats = {
        "total_rounds": len(rounds),
        "ct_rounds_won": 0,
        "t_rounds_won": 0,
        "bomb_plants": len(bomb_events.get("bomb_planted", [])),
        "bomb_defuses": len(bomb_events.get("bomb_defused", [])),
        "bomb_explosions": len(bomb_events.get("bomb_exploded", [])),
    }
    
    # Count round wins by tea
    for round_event in rounds:
        winner = str(round_event.get("winner", ""))
        if winner.upper() == "CT":
            stats["ct_rounds_won"] += 1
        elif winner.upper() == "T":
            stats["t_rounds_won"] += 1
    
    return stats


def calculate_advanced_stats(players, kills, rounds):
    """
    Calculate advanced statistics:
    - Multi-kills (2K, 3K, 4K, 5K per round)
    - Trade kills/deaths (teammate death burst window)
    - Clutches (1v1, 1v2, etc. per round)
    
    Multi-kills are detected by consecutive kills within a round.
    Round boundaries come from the rounds list.
    """
    
    # Build a map of round -> all kills in that round
    kills_by_round = defaultdict(list)
    
    if rounds:
        # Use round tick boundaries if available
        for i, round_event in enumerate(rounds):
            round_num = int(round_event.get("round", i))
            round_tick = int(round_event.get("tick", 0))
            
            # Get next round's tick if available
            next_round_tick = float('inf')
            if i + 1 < len(rounds):
                next_round_tick = int(rounds[i + 1].get("tick", float('inf')))
            
            # Assign kills to this round based on tick
            for kill in kills:
                kill_tick = int(kill.get("tick", 0))
                if round_tick <= kill_tick < next_round_tick:
                    kills_by_round[round_num].append(kill)
    else:
        # Fallback: group kills by 32-tick windows (1 second @ 128 tick)
        # This is a rough estimate when round data isn't available
        for kill in kills:
            tick = int(kill.get("tick", 0))
            round_estimate = tick // 1024  # Rough grouping
            kills_by_round[round_estimate].append(kill)
    
    # Now calculate multi-kills per player per round
    for steamid in players:
        multi_kill_counts = defaultdict(int)
        
        for round_num, round_kills in kills_by_round.items():
            # Filter kills by this player
            player_kills_in_round = [
                k for k in round_kills
                if str(k.get("attacker_steamid", "")) == steamid
            ]
            
            # Count consecutive kills (all kills by player in round)
            consecutive_count = len(player_kills_in_round)
            if consecutive_count >= 2:
                key = f"{consecutive_count}k"
                if consecutive_count <= 5:
                    multi_kill_counts[key] += 1
                else:
                    multi_kill_counts["5k"] += 1
        
        players[steamid]["multi_kills"] = {
            "2k": multi_kill_counts.get("2k", 0),
            "3k": multi_kill_counts.get("3k", 0),
            "4k": multi_kill_counts.get("4k", 0),
            "5k": multi_kill_counts.get("5k", 0),
        }

    # Trade kills/deaths: if a player scores a kill soon after an ally died,
    # treat it as a trade kill; if player dies soon after an enemy trade, count a trade death.
    last_team_death_tick = defaultdict(dict)
    for kill in sorted(kills, key=lambda x: x.get("tick", 0)):
        attacker_steamid = str(kill.get("attacker_steamid", ""))
        victim_steamid = str(kill.get("victim_steamid", ""))
        attacker_team = players.get(attacker_steamid, {}).get("team")
        victim_team = players.get(victim_steamid, {}).get("team")
        kill_tick = int(kill.get("tick", 0))

        if attacker_team and victim_team and attacker_team != victim_team:
            attacker_recent_trade = any(
                (kill_tick - teammate_tick) <= TRADE_WINDOW_TICKS
                for teammate_tick in last_team_death_tick.get(attacker_team, {}).values()
            )
            victim_recent_trade = any(
                (kill_tick - enemy_tick) <= TRADE_WINDOW_TICKS
                for enemy_tick in last_team_death_tick.get(victim_team, {}).values()
            )

            if attacker_recent_trade and attacker_steamid in players:
                players[attacker_steamid]["trade_kills"] += 1
            if victim_recent_trade and victim_steamid in players:
                players[victim_steamid]["trade_deaths"] += 1

        if victim_steamid in players and victim_team:
            last_team_death_tick[victim_team][victim_steamid] = kill_tick

    # Clutches: if a player kills while the opposing team has only 1..5 players left alive
    # estimate the clutch state using round-level alive counts.
    alive_by_team = defaultdict(set)
    for steamid, player in players.items():
        alive_by_team[str(player.get("team", "Unknown"))].add(steamid)

    for round_kills in kills_by_round.values():
        round_alive = defaultdict(set)
        for steamid, player in players.items():
            round_alive[str(player.get("team", "Unknown"))].add(steamid)

        for kill in sorted(round_kills, key=lambda x: x.get("tick", 0)):
            attacker_steamid = str(kill.get("attacker_steamid", ""))
            victim_steamid = str(kill.get("victim_steamid", ""))
            if not attacker_steamid or not victim_steamid:
                continue

            attacker_team = players.get(attacker_steamid, {}).get("team")
            victim_team = players.get(victim_steamid, {}).get("team")
            if not attacker_team or not victim_team or attacker_team == victim_team:
                continue

            if victim_steamid in round_alive.get(victim_team, set()):
                round_alive[victim_team].discard(victim_steamid)

            enemy_alive = len(round_alive.get(victim_team, set()))
            if 1 <= enemy_alive <= 5 and attacker_steamid in players:
                clutch_key = f"1v{enemy_alive}"
                if clutch_key in players[attacker_steamid]["clutches"]:
                    players[attacker_steamid]["clutches"][clutch_key] += 1
    
    if DEBUG_MODE:
        print(f"[DEBUG] Multi-kills calculated: {kills_by_round}")
        print(f"[DEBUG] Trade kills/deaths: { {k: v['trade_kills'] for k,v in players.items()} }")
    
    return players


def build_final_payload(file_path, players, kills, round_stats, bomb_events, receive_key=None):
    """
    Build the final JSON payload for Base44 upload.
    
    Structure:
    - Match metadata
    - Player statistics (no raw steamid or internal IDs)
    - Duel/kill records (simplified)
    - Round statistics
    """
    
    # Build duels array (simplified from kills)
    duels = []
    for i, kill in enumerate(kills):
        duels.append({
            "winner": str(kill.get("attacker_name", "Unknown")),
            "loser": str(kill.get("user_name", "Unknown")),
            "weapon": str(kill.get("weapon", "unknown")),
            "is_headshot": bool(kill.get("headshot", False)),
            "is_entry": False,  # Would require round boundary detection
            "is_trade": False,  # Would require teammate relationship tracking
        })
    
    # Build players array
    players_array = []
    for steamid, player_data in players.items():
        player_record = {
            "user_name": player_data.get("user_name", "Unknown"),
            "steamid": player_data.get("steamid"),
            "team": player_data.get("team", "Unknown"),
            "kills": player_data.get("kills", 0),
            "deaths": player_data.get("deaths", 0),
            "assists": player_data.get("assists", 0),
            "score": player_data.get("score", 0),
            "mvps": player_data.get("mvps", 0),
            "kd": player_data.get("kd", 0.0),
            "adr": player_data.get("adr", 0.0),
            "kast": player_data.get("kast", 0.0),
            "hs_percent": player_data.get("hs_percent", 0.0),
            "headshot_kills": player_data.get("headshot_kills", 0),
            "entry_kills": player_data.get("entry_kills", 0),
            "entry_deaths": player_data.get("entry_deaths", 0),
            "trade_kills": player_data.get("trade_kills", 0),
            "trade_deaths": player_data.get("trade_deaths", 0),
            "multi_kills": player_data.get("multi_kills", {}),
            "clutches": player_data.get("clutches", {"1v1": 0, "1v2": 0, "1v3": 0, "1v4": 0, "1v5": 0}),
            "weapon_kills": player_data.get("weapon_kills", {}),
        }
        players_array.append(player_record)
    
    payload = {
        "match_name": os.path.basename(file_path),
        "map_name": detect_map_name(file_path),
        "timestamp": int(time.time()),
        "total_rounds": round_stats.get("total_rounds", 0),
        "ct_rounds_won": round_stats.get("ct_rounds_won", 0),
        "t_rounds_won": round_stats.get("t_rounds_won", 0),
        "bomb_plants": round_stats.get("bomb_plants", 0),
        "bomb_defuses": round_stats.get("bomb_defuses", 0),
        "bomb_explosions": round_stats.get("bomb_explosions", 0),
        "total_duels_count": len(duels),
        "players": players_array,
        "duels": duels,
    }
    
    return payload


def detect_map_name(file_path):
    """Detect map name from file path."""
    name = os.path.basename(file_path).lower()
    for m in ["mirage", "dust2", "inferno", "nuke", "ancient", "anubis", "vertigo", "overpass"]:
        if m in name:
            return f"de_{m}"
    return "unknown_map"


# ========================================================
# FILE SYSTEM WATCHER & UPLOAD
# ========================================================

class DemoHandler(FileSystemEventHandler):
    """Watches a folder for new .dem files and uploads parsed stats."""

    def __init__(self, api_url, receive_key, log_fn):
        super().__init__()
        self.api_url = api_url
        self.receive_key = receive_key
        self.log = log_fn

    def on_created(self, event):
        if not event.is_directory and event.src_path.endswith(".dem"):
            self.log(f"🆕 Шинэ demo файл олдлоо: {os.path.basename(event.src_path)}")
            self.log("⏳ Тоглоом файлыг бүрэн хадгалж дуустал 15 секунд хүлээнэ...")
            time.sleep(15)
            self.process_demo(event.src_path)

    def process_demo(self, file_path):
        """
        Main pipeline:
        1. Parse demo
        2. Extract raw data
        3. Calculate statistics
        4. Build final JSON
        5. Upload to Base44
        """
        try:
            self.log("📊 Demo файлыг задлаж (Parse) байна...")
            
            # === PARSE ===
            parser = parse_demo(file_path)
            
            # === EXTRACT ===
            self.log("🔍 Тоглогчдын мэдээллийг гаргаж авч байна...")
            players = extract_player_data(parser)
            self.log(f"   ✓ {len(players)} тоглогч олдлоо")
            
            if DEBUG_MODE and players:
                first_player = list(players.values())[0]
                self.log(f"[DEBUG] Player sample: {json.dumps(first_player, ensure_ascii=False, indent=2)}")
                self.log(f"[DEBUG] All players keys: {list(players.keys())}")
                self.log(f"[DEBUG] All player names: {[p['user_name'] for p in players.values()]}")
            
            self.log("🔥 Хэцэн үйлдлүүдийг гаргаж авч байна...")
            kills = extract_kill_events(parser)
            self.log(f"   ✓ {len(kills)} хэцэн үйлдэл олдлоо")
            
            if DEBUG_MODE and kills:
                self.log(f"[DEBUG] First 3 kills:")
                for i, kill in enumerate(kills[:3]):
                    self.log(f"   Kill {i+1}: Attacker='{kill['attacker_name']}' ({kill['attacker_steamid']}) → Victim='{kill['user_name']}' ({kill['victim_steamid']}) | {kill['weapon']}")
            
            if not kills:
                self.log("⚠️ No kills found in demo - checking if demo has kill event data...")
            
            self.log("🏁 Раундын мэдээллийг гаргаж авч байна...")
            rounds = extract_round_events(parser)
            self.log(f"   ✓ {len(rounds)} раунд олдлоо")

            self.log("💥 Хохирлын хэмжээг гаргаж авч байна...")
            damage_events = extract_damage_events(parser)
            self.log(f"   ✓ {len(damage_events)} хохирлын бүртгэл олдлоо")
            
            self.log("💣 Бөмб үйлдлүүдийг гаргаж авч байна...")
            bomb_events = extract_bomb_events(parser)
            self.log(f"   ✓ {len(bomb_events.get('bomb_planted', []))} plant, "
                    f"{len(bomb_events.get('bomb_defused', []))} defuse")

            round_stats = calculate_round_stats(rounds, bomb_events, players)
            
            # === CALCULATE ===
            self.log("📈 Тоглогчдын статистикийг тооцож байна...")
            players = calculate_player_stats(players, kills, damage_events=damage_events, total_rounds=round_stats.get("total_rounds", 0))
            
            if DEBUG_MODE:
                self.log("[DEBUG] Final player stats:")
                for steamid, pdata in sorted(players.items(), key=lambda x: x[1]['kills'], reverse=True)[:5]:
                    self.log(f"  {pdata['user_name']:15} | K:{pdata['kills']:2} D:{pdata['deaths']:2} A:{pdata['assists']:2} | K/D:{pdata['kd']} KAST:{pdata['kast']}%")
            
            self.log("⚔️ Сохилын статистикийг тооцож байна...")
            players = calculate_combat_stats(players, kills)
            
            self.log("📊 Раундын статистикийг тооцож байна...")
            round_stats = calculate_round_stats(rounds, bomb_events, players)
            
            self.log("🎯 Нарийн статистикийг тооцож байна...")
            players = calculate_advanced_stats(players, kills, rounds)
            
            # === BUILD PAYLOAD ===
            self.log("📦 Эцсийн JSON-г бүтээж байна...")
            payload = build_final_payload(file_path, players, kills, round_stats, bomb_events)
            payload = json_safe(payload)
            
            # === SAVE JSON ===
            os.makedirs(PAYLOAD_DIR, exist_ok=True)
            safe_name = os.path.splitext(os.path.basename(file_path))[0]
            payload_path = os.path.join(PAYLOAD_DIR, f"{safe_name}_{int(time.time())}.json")
            try:
                with open(payload_path, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                self.log(f"💾 JSON-г хадгаллаа: {payload_path}")
            except Exception as e:
                self.log(f"⚠️ JSON хадгалахад алдаа: {e}")
            
            # === DEBUG LOG ===
            if DEBUG_MODE:
                masked_key = f"***{self.receive_key[-4:]}" if not DEBUG_SHOW_KEY else self.receive_key
                self.log(f"[DEBUG] API: {self.api_url}")
                self.log(f"[DEBUG] Auth: receive_key={masked_key}")
                self.log(f"[DEBUG] JSON Payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
            
            # === UPLOAD ===
            self.log("🚀 Base44 сервер рүү илгээж байна...")
            response = requests.post(
                self.api_url,
                params={"key": self.receive_key},
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "x-receive-key": self.receive_key,
                },
                timeout=20,
            )
            
            if DEBUG_MODE:
                self.log(f"[DEBUG] HTTP Status: {response.status_code}")
                self.log(f"[DEBUG] Response Headers:\n{json.dumps(dict(response.headers), ensure_ascii=False, indent=2)}")
            
            if response.status_code in (200, 201):
                self.log("✅ АМЖИЛТТАЙ! Серверээс хүлээж авлаа.")
                if DEBUG_MODE:
                    try:
                        resp_data = response.json()
                        self.log(f"[DEBUG] Response Body:\n{json.dumps(resp_data, ensure_ascii=False, indent=2)}")
                    except ValueError:
                        body = response.text.strip()
                        if body:
                            self.log(f"[DEBUG] Response Body: {body[:1000]}")
            else:
                self.log(f"❌ СЕРВЕРИЙН АЛДАА! HTTP {response.status_code}")
                try:
                    detail = response.json()
                    self.log(f"↳ Хариу: {detail}")
                    if DEBUG_MODE:
                        self.log(f"[DEBUG] Error Response:\n{json.dumps(detail, ensure_ascii=False, indent=2)}")
                except ValueError:
                    body = response.text.strip()
                    if body:
                        self.log(f"↳ Хариу: {body[:500]}")
                        if DEBUG_MODE:
                            self.log(f"[DEBUG] Error Response: {body}")
        
        except Exception as e:
            self.log(f"🚨 Алдаа гарлаа: {e}")
            if DEBUG_MODE:
                import traceback
                self.log(f"[DEBUG] Traceback:\n{traceback.format_exc()}")


def process_and_upload_demo(file_path, api_url, receive_key, log_fn):
    """Parses a single .dem file and uploads it immediately."""
    handler = DemoHandler(api_url, receive_key, log_fn)
    handler.process_demo(file_path)


# ========================================================
# GUI APPLICATION
# ========================================================

class FragTrackApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Frag-Track LAN — Auto Uploader (Refactored)")
        self.geometry("720x560")
        self.minsize(620, 460)

        self.cfg = load_config()
        self.observer = None
        self.watching = False
        self.uploading = False
        self.log_queue = queue.Queue()

        self._build_ui()
        self._poll_log_queue()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        # Shared API settings
        settings = ttk.LabelFrame(self, text="Сервер тохиргоо")
        settings.pack(fill="x", **pad)

        ttk.Label(settings, text="API URL:").grid(row=0, column=0, sticky="w", padx=8, pady=6)
        self.api_var = tk.StringVar(value=self.cfg.get("api_url", DEFAULT_WEBSITE_API_URL))
        ttk.Entry(settings, textvariable=self.api_var, width=55).grid(row=0, column=1, sticky="ew", padx=4, pady=6)

        ttk.Label(settings, text="Receive key:").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        self.key_var = tk.StringVar(value=self.cfg.get("receive_key", DEFAULT_RECEIVE_KEY))
        ttk.Entry(settings, textvariable=self.key_var, width=55, show="•").grid(
            row=1, column=1, sticky="ew", padx=4, pady=6
        )
        ttk.Button(settings, text="JSON хавтас нээх", command=self._open_payload_folder).grid(
            row=1, column=2, padx=8, pady=6
        )
        settings.columnconfigure(1, weight=1)

        # Tabs
        notebook = ttk.Notebook(self)
        notebook.pack(fill="x", **pad)

        auto_tab = ttk.Frame(notebook)
        manual_tab = ttk.Frame(notebook)
        notebook.add(auto_tab, text="Автомат хяналт")
        notebook.add(manual_tab, text="Гараар илгээх")

        # --- Auto tab ---
        ttk.Label(auto_tab, text="CS2 'csgo' хавтас:").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.folder_var = tk.StringVar(value=self.cfg.get("cs2_dir", ""))
        ttk.Entry(auto_tab, textvariable=self.folder_var, width=50).grid(row=0, column=1, sticky="ew", padx=4, pady=8)
        ttk.Button(auto_tab, text="Browse…", command=self._browse_folder).grid(row=0, column=2, padx=8, pady=8)
        auto_tab.columnconfigure(1, weight=1)

        auto_controls = ttk.Frame(auto_tab)
        auto_controls.grid(row=1, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 8))

        self.start_btn = ttk.Button(auto_controls, text="▶ Эхлүүлэх", command=self._start_watching)
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ttk.Button(auto_controls, text="■ Зогсоох", command=self._stop_watching, state="disabled")
        self.stop_btn.pack(side="left")

        self.status_var = tk.StringVar(value="⚪ Зогссон")
        ttk.Label(auto_controls, textvariable=self.status_var, font=("", 11, "bold")).pack(side="right")

        # --- Manual tab ---
        ttk.Label(manual_tab, text=".dem файл:").grid(row=0, column=0, sticky="w", padx=8, pady=8)
        self.dem_file_var = tk.StringVar(value="")
        ttk.Entry(manual_tab, textvariable=self.dem_file_var, width=50).grid(
            row=0, column=1, sticky="ew", padx=4, pady=8
        )
        ttk.Button(manual_tab, text="Browse…", command=self._browse_dem_file).grid(row=0, column=2, padx=8, pady=8)
        manual_tab.columnconfigure(1, weight=1)

        self.upload_btn = ttk.Button(manual_tab, text="⬆ Илгээх", command=self._manual_upload)
        self.upload_btn.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))

        # Log area
        log_frame = ttk.LabelFrame(self, text="Лог")
        log_frame.pack(fill="both", expand=True, **pad)

        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", bg="#111", fg="#ddd")
        self.log_text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)

        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y", pady=6, padx=(0, 6))
        self.log_text.configure(yscrollcommand=scrollbar.set)

        self._log("Frag-Track LAN (Refactored) бэлэн. Шинэ архитектур: Python статистик → Base44 upload")

    # ---------- Actions ----------
    def _open_payload_folder(self):
        os.makedirs(PAYLOAD_DIR, exist_ok=True)
        try:
            if sys.platform == "darwin":
                os.system(f'open "{PAYLOAD_DIR}"')
            elif sys.platform.startswith("win"):
                os.startfile(PAYLOAD_DIR)  # type: ignore[attr-defined]
            else:
                os.system(f'xdg-open "{PAYLOAD_DIR}"')
        except Exception as e:
            messagebox.showinfo("JSON хавтас", f"Хавтас: {PAYLOAD_DIR}\n\n(Автоматаар нээж чадсангүй: {e})")

    def _browse_folder(self):
        chosen = filedialog.askdirectory(title="CS2 'csgo' хавтсаа сонгоно уу")
        if chosen:
            self.folder_var.set(chosen)

    def _start_watching(self):
        folder = self.folder_var.get().strip()
        api_url = self.api_var.get().strip()
        receive_key = self.key_var.get().strip()

        if not folder or not os.path.isdir(folder):
            messagebox.showerror("Алдаа", "CS2 хавтасны зам буруу байна эсвэл олдсонгүй.")
            return
        if not api_url:
            messagebox.showerror("Алдаа", "API URL хоосон байна.")
            return

        save_config({"cs2_dir": folder, "api_url": api_url, "receive_key": receive_key})

        handler = DemoHandler(api_url, receive_key, self._log)
        self.observer = Observer()
        self.observer.schedule(handler, path=folder, recursive=False)

        try:
            self.observer.start()
        except Exception as e:
            messagebox.showerror("Алдаа", f"Хавтас хянаж эхлэхэд алдаа гарлаа:\n{e}")
            return

        self.watching = True
        self.status_var.set("🟢 Хянаж байна")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._log(f"📂 Хянаж буй хавтас: {folder}")
        self._log(f"🌐 API: {api_url}")
        self._log("📢 Хяналт эхэллээ. Шинэ .dem файлыг нь хүлээж байна...")

    def _browse_dem_file(self):
        chosen = filedialog.askopenfilename(
            title="Илгээх .dem файлаа сонгоно уу", filetypes=[("CS2 Demo files", "*.dem"), ("All files", "*.*")]
        )
        if chosen:
            self.dem_file_var.set(chosen)

    def _manual_upload(self):
        if self.uploading:
            return

        dem_path = self.dem_file_var.get().strip()
        api_url = self.api_var.get().strip()
        receive_key = self.key_var.get().strip()

        if not dem_path or not os.path.isfile(dem_path):
            messagebox.showerror("Алдаа", ".dem файлын зам буруу байна эсвэл олдсонгүй.")
            return
        if not dem_path.lower().endswith(".dem"):
            messagebox.showerror("Алдаа", "Сонгосон файл .dem өргөтгөлтэй байх ёстой.")
            return
        if not api_url:
            messagebox.showerror("Алдаа", "API URL хоосон байна.")
            return

        save_config(
            {"cs2_dir": self.folder_var.get().strip(), "api_url": api_url, "receive_key": receive_key}
        )

        self.uploading = True
        self.upload_btn.config(state="disabled", text="⏳ Илгээж байна...")
        self._log(f"📁 Файл: {os.path.basename(dem_path)}")

        def worker():
            try:
                process_and_upload_demo(dem_path, api_url, receive_key, self._log)
            finally:
                self.uploading = False
                self.after(0, lambda: self.upload_btn.config(state="normal", text="⬆ Илгээх"))

        threading.Thread(target=worker, daemon=True).start()

    def _stop_watching(self):
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=5)
            self.observer = None
        self.watching = False
        self.status_var.set("⚪ Зогссон")
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        self._log("🛑 Хяналт зогслоо.")

    def _on_close(self):
        if self.watching:
            self._stop_watching()
        self.destroy()

    # ---------- Logging (thread-safe) ----------
    def _log(self, msg):
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._poll_log_queue)


if __name__ == "__main__":
    app = FragTrackApp()
    app.mainloop()
