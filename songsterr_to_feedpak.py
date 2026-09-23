# Copyright (c) 2026 Kevin Lu

from typing import Awaitable, Callable, Generator, Optional, Union, NamedTuple
import argparse
import asyncio
import contextlib
import copy
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import traceback
import zipfile

# External dependencies
import bs4
import ffmpeg
import httpx
import yaml
import yt_dlp

# songsterr_to_feedpak version number to be embedded in the feedpak manifest
CONFIG_SONGSTERR_TO_FEEDPAK_VERSION = "1.0.0"
# Minimum number of frets in anchor
CONFIG_ANCHOR_MIN_WIDTH = 4
# Amount of sustain to remove at the end, measured in beats
CONFIG_SUSTAIN_MARGIN_BEATS = 0.2
# Number of frets to slide for unpitched slides
CONFIG_UNPITCHED_SLIDE_WIDTH = 5
# Duration of preview audio clip
CONFIG_PREVIEW_SECS = 30
# Duration of default generated sections if no sections are present, measured in measures
CONFIG_DEFAULT_SECTION_MEASURES = 32
# For empty section substitution, the number of measures at the start and end
# of the section that can be ignored when determining if the section is empty.
CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES = 2

################################################################
# Utilities
################################################################

_INVALID_FILENAME_CHARS = '<>:"/\\|?*' + ''.join(chr(i) for i in range(32))

def get_note_name(note: int, use_sharps: bool=False) -> str:
    """
    Get the name of a note.
    Notes are numbered from 0 = C0 and increment per semitone.
    """
    return [
        ("C" , "C" ),
        ("Db", "C#"),
        ("D" , "D" ),
        ("Eb", "D#"),
        ("E" , "E" ),
        ("F" , "F" ),
        ("Gb", "F#"),
        ("G" , "G" ),
        ("Ab", "G#"),
        ("A" , "A" ),
        ("Bb", "A#"),
        ("B" , "B" ),
    ][note % 12][int(use_sharps)]

def _round(x: float) -> int:
    """
    Round to the nearest integer.
    Midpoint are rounded away from zero.
    """
    if x >= 0.0:
        return math.floor(x + 0.5)
    else:
        return math.ceil(x - 0.5)

def _mode(values: list[int]) -> int:
    """
    Return the mode of a list of values.
    If there are multiple modes, return the first one found.
    If the list is empty, return 0.
    """
    if not values:
        return 0
    counts = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts, key=counts.get)

def _list_find(lst: list, predicate: Callable[[any], bool]) -> Optional[any]:
    """
    Find the first item in a list that satisfies the predicate.
    If no item is found, return None.
    """
    for item in lst:
        if predicate(item):
            return item
    return None

def _note_a_freq_to_cent_offset(note_a_freq: int) -> int:
    """
    Convert a frequency to a cent offset from A4.
    """
    return _round(1200 * math.log2(note_a_freq / 440))

def _cents_to_freq_ratio(cents: int) -> float:
    """
    Convert a cent offset to a frequency ratio.
    """
    return 2 ** (cents / 1200)

def _cents_improper_to_mixed(cents: int) -> tuple[int, int]:
    """
    Convert a cent offset to a semitone offset and cent remainder.
    """
    semitones = int(_round(cents / 100))
    remainder_cents = cents - (semitones * 100)
    return semitones, remainder_cents

def _to_valid_filename(name: str) -> str:
    """
    Convert a string to a valid filename, using Windows as the common denominator.
    """

    # Replace pipes with dashes for style purposes
    name = name.replace("|", "-")

    # Remove invalid characters
    s = "".join(c for c in name if c not in _INVALID_FILENAME_CHARS).strip()

    # Fix up reserved names
    no_ext = s.split('.')[0].upper()
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$', no_ext):
        s = "_" + s

    # Fix up trailing dots (spaces are already stripped)
    while s.endswith("."):
        s = s[:-1]

    # Fix up empty names
    return s or "file"

def _sign_string(x: int) -> str:
    return f"+{x}" if x > 0 else str(x)

################################################################
# Tuning
################################################################

class _TuningShape:
    """
    Represents a tuning shape (list of semitone deltas between strings, bottom string first).
    A formatter is included to generate a human-readable name for the tuning
    given the actual string notes (bottom string first).
    """

    def __init__(self, deltas: list[int], formatter: Callable[[list[int]], str]):
        self.deltas = deltas
        self.formatter = formatter

_TuningShape.STD = _TuningShape([-5, -4, -5, -5, -5],
                    lambda strings: f"{get_note_name(strings[5])} STD")
_TuningShape.DROP = _TuningShape([-5, -4, -5, -5, -7],
                    lambda strings: f"{get_note_name(strings[0])} DROP {get_note_name(strings[5])}")
_TuningShape.BASS_STD = _TuningShape([-5, -5, -5],
                    lambda strings: f"Bass {get_note_name(strings[3])} STD")
_TuningShape.COMMON_SHAPES = [
    _TuningShape.STD,
    _TuningShape.DROP,
    _TuningShape.BASS_STD,
]

class Tuning:
    """
    A tuning defined by a list of string notes, bottom string first.
    Notes are numbered from 0 = C0 and increment per semitone.
    """

    def __init__(self, strings: list[int]):
        self.strings = strings
        deltas = [strings[i + 1] - strings[i] for i in range(len(strings) - 1)]
        for shape in _TuningShape.COMMON_SHAPES:
            if deltas == shape.deltas:
                # Carry over the value stored in shape to be used out of the loop
                break
        else:
            shape = _TuningShape(deltas,
                                 lambda strings: " ".join(get_note_name(n) for n in reversed(strings)))
        self.name = shape.formatter(strings)

    def add_semitones(self, semitones: int) -> "Tuning":
        return Tuning([n + semitones for n in self.strings])

################################################################
# Songsterr web API
################################################################

def _get_song_url(song_id: int) -> str:
    return f"https://www.songsterr.com/a/wsa/s{song_id}"

def _get_track_url(song: str, revision: str, image: str, part: int) -> str:
    return f"https://dqsljvtekg760.cloudfront.net/{song}/{revision}/{image}/{part}.json"

def _get_video_sync_url(song: str, revision: str) -> str:
    return f"https://www.songsterr.com/api/video-points/{song}/{revision}/list"

def _get_search_url(query: str, from_index: int, count: int) -> str:
    return f"https://www.songsterr.com/api/search?pattern={query}&size={count}&from={from_index}"

async def _fetch(url) -> str:
    print(f"Fetching {url}")
    async with httpx.AsyncClient() as client:
        response = await client.get(url, follow_redirects=True)
        return response.text

async def _fetch_bytes(url) -> bytes:
    print(f"Fetching {url}")
    async with httpx.AsyncClient() as client:
        response = await client.get(url, follow_redirects=True)
        return response.content

class _SongData(NamedTuple):
    song_id: int
    revision: int
    image: str
    names: list[str]
    instruments: list[int]
    instrument_names: list[str]
    track_difficulties: list[Optional[int]]
    track_hashes: list[str]
    tags: list[str]
    artist: str
    title: str

def _extract_song_data(html_text: str) -> _SongData:
    def get_instrument(track: dict) -> str:
        if track.get("isVocalTrack"):
            return Instrument.VOCALS
        elif track.get("isDrums"):
            return Instrument.DRUMS
        elif track.get("isBassGuitar"):
            return Instrument.BASS
        elif track.get("isGuitar"):
            return Instrument.GUITAR
        elif track.get("isPiano"):
            return Instrument.PIANO
        else:
            return Instrument.NONE

    if "Too Many Requests" in html_text:
        raise ValueError("Songsterr API rate limit exceeded.")
    soup = bs4.BeautifulSoup(html_text, "html.parser")
    state = soup.find(id="state")
    if state is None:
        raise ValueError("Could not find state in HTML")
    j = state.text
    data = json.loads(j)["meta"]["current"]
    return _SongData(
        song_id=data["songId"],
        revision=data["revisionId"],
        image=data["image"],
        names=[t["name"] for t in data["tracks"]],
        instruments=[get_instrument(t) for t in data["tracks"]],
        instrument_names=[t["instrument"] for t in data["tracks"]],
        track_difficulties=[t.get("difficulty") for t in data["tracks"]],
        track_hashes=[t["hash"] for t in data["tracks"]],
        tags=data["tags"],
        artist=data["artist"],
        title=data["title"],
    )

class _TrackData(NamedTuple):
    string_count: int
    tuning: Optional[Tuning]
    capo: int
    measures: list[dict]
    note_a_freq: int

def _extract_track_data(json_text: str) -> _TrackData:
    match = re.search(r"\b(\d+) ?[Hh][Zz]\b", json_text)
    note_a_freq = int(match.group(1)) if match else 440
    json_data = json.loads(json_text)
    return _TrackData(
        string_count=json_data["strings"],
        tuning=Tuning(json_data["tuning"]) if json_data.get("tuning") else None,
        capo=json_data.get("capo", 0),
        measures=json_data["measures"],
        note_a_freq=note_a_freq,
    )

class _VideoSyncData(NamedTuple):
    video_id: str
    video_type: str
    compatible_track_hashes: list[str]
    measure_times: list[float]

class VideoType:
    MAIN = "main"
    MAIN_ALT = "alternative"
    BACKING = "backing"
    SOLO = "solo"
    PLAYTHROUGH = "playthrough"

def _extract_video_sync_data(json_text: str) -> list[_VideoSyncData]:
    videos = json.loads(json_text)
    if "code" in videos and videos["code"] == "ERR_TOO_MANY_REQUESTS":
        raise ValueError("Songsterr API rate limit exceeded.")
    if videos is None:
        return []

    def get_video_type(video: dict) -> str:
        feature = video.get("feature")
        if feature == "alternative":
            return VideoType.MAIN_ALT
        elif feature == "backing":
            return VideoType.BACKING
        elif feature == "solo":
            return VideoType.SOLO
        elif feature == "playthrough":
            return VideoType.PLAYTHROUGH
        return VideoType.MAIN

    video_sync_data = [_VideoSyncData(
        video_id=video["videoId"],
        video_type=get_video_type(video),
        compatible_track_hashes=video["trackHashes"],
        measure_times=video["points"],
    ) for video in videos]

    order = [
        VideoType.MAIN,
        VideoType.MAIN_ALT,
        VideoType.BACKING,
        VideoType.SOLO,
        VideoType.PLAYTHROUGH,
    ]
    return sorted(video_sync_data, key=lambda v: order.index(v.video_type))

class Instrument:
    NONE = 0
    GUITAR = 1 << 1
    RHYTHM_GUITAR = 1 << 2
    LEAD_GUITAR = 1 << 3
    BASS = 1 << 4
    DRUMS = 1 << 5
    PIANO = 1 << 6
    VOCALS = 1 << 7

class SongsterrTrackSearchResult(NamedTuple):
    name: str
    instrument_name: str
    tuning: Optional[Tuning]
    difficulty: Optional[int]

    def get_name(self) -> str:
        return self.name or self.instrument_name

class SongsterrSongSearchResult(NamedTuple):
    title: str
    artist: str
    song_id: int
    tracks: list[SongsterrTrackSearchResult]

def _extract_songsterr_search_results(json_text: str) -> list[SongsterrSongSearchResult]:
    search_results = json.loads(json_text)
    results = []
    for result in search_results["records"]:
        tracks = []
        for track in result["tracks"]:
            tracks.append(SongsterrTrackSearchResult(
                name=track["name"],
                instrument_name=track["instrument"],
                tuning=Tuning(track["tuning"]) if track.get("tuning") else None,
                difficulty=track.get("difficulty"),
            ))
        results.append(SongsterrSongSearchResult(
            title=result["title"],
            artist=result["artist"],
            song_id=result["songId"],
            tracks=tracks,
        ))
    return results

class SongsterrTrack(NamedTuple):
    track_id: int
    name: str
    instrument: int
    instrument_name: str
    tuning: Optional[Tuning]
    capo: int
    difficulty: Optional[int]
    track_hash: str
    measures: list[dict]
    cent_offset: int

    def get_name(self) -> str:
        return self.name or self.instrument_name

class SongsterrSong(NamedTuple):
    song_id: int
    title: str
    artist: str
    tracks: list[SongsterrTrack]
    yt_video_id: str
    yt_video_type: str
    compatible_track_hashes: list[str]
    video_sync_times: list[float]

async def search_songsterr(query: str, from_index: int=0, count: int=5) -> list[SongsterrSongSearchResult]:
    search_url = _get_search_url(query, from_index, count)
    search_results = await _fetch(search_url)
    return _extract_songsterr_search_results(search_results)

async def download_songsterr_song(song_id: int) -> list[SongsterrSong]:
    html_url = _get_song_url(song_id)
    html_text = await _fetch(html_url)
    song_data = _extract_song_data(html_text)

    video_sync_url = _get_video_sync_url(song_data.song_id, song_data.revision)
    video_sync_datas = _extract_video_sync_data(await _fetch(video_sync_url))

    download_tasks = []
    for i in range(len(song_data.names)):
        track_url = _get_track_url(song_data.song_id, song_data.revision, song_data.image, i)
        download_tasks.append(_fetch(track_url))
    track_data_text = await asyncio.gather(*download_tasks)
    track_datas = [_extract_track_data(text) for text in track_data_text]

    songs = []
    for video_sync_data in video_sync_datas:
        song = SongsterrSong(
            song_id=song_data.song_id,
            title=song_data.title,
            artist=song_data.artist,
            tracks=[],
            yt_video_id=video_sync_data.video_id,
            yt_video_type=video_sync_data.video_type,
            compatible_track_hashes=video_sync_data.compatible_track_hashes,
            video_sync_times=video_sync_data.measure_times,
        )
        zipped = zip(song_data.names,
                     song_data.instruments,
                     song_data.instrument_names,
                     song_data.track_difficulties,
                     song_data.track_hashes,
                     track_datas)
        for i, (name, instrument, instrument_name, track_difficulty, track_hash, track_data) in enumerate(zipped):
            if instrument & Instrument.GUITAR:
                def match(keyword: str) -> bool:
                    return (re.search(f"\\b{keyword}\\b", name, flags=re.IGNORECASE)
                        or re.search(f"\\b{keyword}\\b", instrument_name, flags=re.IGNORECASE))
                if match("lead") or match("solo"):
                    instrument |= Instrument.LEAD_GUITAR
                if match("rhythm"):
                    instrument |= Instrument.RHYTHM_GUITAR

            song.tracks.append(SongsterrTrack(
                track_id=i,
                name=name,
                instrument=instrument,
                instrument_name=instrument_name,
                tuning=track_data.tuning,
                difficulty=track_difficulty,
                track_hash=track_hash,
                capo=track_data.capo,
                measures=track_data.measures,
                cent_offset=_note_a_freq_to_cent_offset(track_data.note_a_freq),
            ))
        songs.append(song)
    return songs

################################################################
# Youtube downloading
################################################################

class YoutubeDownloadError(Exception):
    pass

class Mp3(NamedTuple):
    data: bytes
    duration: float
    thumbnail: Optional[bytes]
    preview: Optional[bytes]
    retuned_by_cents: int

def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None

def has_ffmpeg_rubberband_filter():
    try:
        result = subprocess.run(
            ["ffmpeg", "-filters"],
            capture_output=True,
            text=True,
            check=True)
        return "rubberband" in result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        return False

async def download_youtube_mp3(video_id: str,
                               include_thumbnail: bool=False,
                               include_preview: bool=False,
                               retune_by_cents: int=0,
                               mock_mp3_path: Optional[str]=None) -> Mp3:
    def work():
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_file =  os.path.join(tmp_dir, video_id)
            ydl_opts = {
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '0',
                }],
                'outtmpl': tmp_file,
            }
            tmp_file += '.mp3'

            print(f"==== BEGIN YOUTUBE DOWNLOAD {video_id} ====")
            try:
                if mock_mp3_path:
                    if include_thumbnail:
                        raise ValueError("thumbnail not supported for mock mp3")
                    shutil.copy(mock_mp3_path, tmp_file)
                    thumbnail = None
                else:
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        if include_thumbnail:
                            info = ydl.extract_info(f'https://www.youtube.com/watch?v={video_id}', download=True)
                            thumbnail_url = info.get('thumbnail')
                            thumbnail = asyncio.run(_fetch_bytes(thumbnail_url) if thumbnail_url else None)
                        else:
                            ydl.download([f'https://www.youtube.com/watch?v={video_id}'])
                            thumbnail = None
            except Exception as e:
                raise YoutubeDownloadError(f"Failed to download YouTube video {video_id}: {e}") from e
            finally:
                print(f"==== END YOUTUBE DOWNLOAD {video_id} ====")

            if retune_by_cents:
                print(f"==== BEGIN RETUNING {retune_by_cents}c ====")
                try:
                    freq_ratio = _cents_to_freq_ratio(retune_by_cents)
                    retuned_file = os.path.join(tmp_dir, "retuned.mp3")
                    ffmpeg.input(tmp_file).output(retuned_file,
                                                af=f"rubberband=pitch={freq_ratio}").run(overwrite_output=True)
                    tmp_file = retuned_file
                finally:
                    print(f"==== END RETUNING {retune_by_cents}c ====")

            duration = float(ffmpeg.probe(tmp_file)['format']['duration'])

            if include_preview:
                print(f"==== BEGIN PREVIEW GENERATION ====")
                try:
                    preview_file = os.path.join(tmp_dir, "preview.mp3")
                    preview_start = max(min(duration / 2, duration - CONFIG_PREVIEW_SECS), 0)
                    preview_duration = min(CONFIG_PREVIEW_SECS, duration)
                    ffmpeg.input(tmp_file).output(preview_file,
                                                ss=preview_start,
                                                t=preview_duration).run(overwrite_output=True)
                    with open(preview_file, 'rb') as f:
                        preview = f.read()
                finally:
                    print(f"==== END PREVIEW GENERATION ====")
            else:
                preview = None

            with open(tmp_file, 'rb') as f:
                return Mp3(data=f.read(),
                           duration=duration,
                           thumbnail=thumbnail,
                           preview=preview,
                           retuned_by_cents=retune_by_cents)
    return await asyncio.to_thread(work)

################################################################
# Feedpak generation
################################################################

def _get_arrangement_filename(track: SongsterrTrack) -> str:
    return f"arrangements/{track.track_id} - {_to_valid_filename(track.get_name())}.json"

def _get_song_timeline_filename() -> str:
    return "song_timeline.json"

def _get_stem_filename(song: SongsterrSong) -> str:
    return f"stems/full_{song.yt_video_id}.mp3"

def _get_cover_filename() -> str:
    return "cover.jpg"

def _get_preview_filename() -> str:
    return "preview.mp3"

def build_feedpak_tuning(tuning: Tuning, is_bass: bool) -> list[int]:
    """
    Convert a tuning to feedpak semitone offsets.
    Songsterr provides string notes from the highest (thinnest) string to the
    lowest (thickest). The feedpak tuning is semitone offsets from the
    instrument's standard open strings, ordered from the lowest string to the
    highest, so we compare against the standard tuning in the same
    high-to-low order and reverse the result at the end.
    """
    guitar_std8 = [64, 59, 55, 50, 45, 40, 35, 30]
    bass_std = {
        4: [43, 38, 33, 28],
        5: [43, 38, 33, 28, 23],
        6: [48, 43, 38, 33, 28, 23],
    }
    reference = bass_std[len(tuning.strings)] if is_bass else guitar_std8
    diff = [s - e for s, e in zip(tuning.strings, reference)]
    return list(reversed(diff))

def _iterate_measures(measures: list) -> Generator[tuple[dict, bool], None, None]:
    """
    Iterate over measures while handling repeats.
    """
    i = 0
    wildcard = object()
    current_alternate_endings = set([wildcard])
    alternate_endings = []

    def finish_repeat(repeat_count: int):
        nonlocal current_alternate_endings
        for repeat_number in range(1, repeat_count + 1):
            for j, cur_ae in enumerate(alternate_endings):
                if next(iter(cur_ae)) is wildcard or repeat_number in cur_ae:
                    yield measures[i - len(alternate_endings) + j + 1], repeat_number > 1
        current_alternate_endings = set([wildcard])
        alternate_endings.clear()

    while i < len(measures):
        measure = measures[i]

        if alternate_endings and "alternateEnding" in measure:
            current_alternate_endings = set(measure["alternateEnding"])

        if measure.get("repeatStart") or alternate_endings:
            alternate_endings.append(current_alternate_endings)

        if not alternate_endings:
            yield measure, False

        if "repeat" in measure:
            yield from finish_repeat(measure["repeat"])
        elif alternate_endings and (
                i + 1 >= len(measures) or
                i + 1 < len(measures) and measures[i + 1].get("repeatStart")):
            # Score incorrectly started a repeat but never ended it.
            yield from finish_repeat(1)
        i += 1

class MeasureInfo(NamedTuple):
    time: float
    secs_per_semibreve: float
    note_index: int
    chord_index: int

class SectionInfo(NamedTuple):
    time: float
    name: str
    measure_index: int

def build_feedpak_arrangement(song: SongsterrSong, track: SongsterrTrack) -> tuple[dict, dict, list[MeasureInfo], list[SectionInfo]]:
    fp_notes = []
    fp_chords = []
    fp_sections = []
    fp_beats = []
    measure_info = []
    section_info = []

    DEFAULT_BPM = 100
    secs_per_semibreve = 15 / DEFAULT_BPM
    t = 0
    hopo_from = {} # Map of string -> the fret we are hopo-ing from
    slides = set() # Strings that are currently sliding
    prev_notes = {} # Map of string -> the previous note on that string

    for measure_num, (measure, is_repeat) in enumerate(_iterate_measures(track.measures)):
        # Calculate the length of measure
        beats = measure["voices"][0]["beats"]
        semibreves_in_measure = sum(
            beat["duration"][0] / beat["duration"][1]
            for beat in beats
        )

        # Update current time
        if measure_num < len(song.video_sync_times):
            t = song.video_sync_times[measure_num]

        # Calculate BPM (actually secs per semibreve since that is more natural)
        if measure_num + 1 < len(song.video_sync_times):
            next_measure_t = song.video_sync_times[measure_num + 1]
        else:
            next_measure_t = t + (semibreves_in_measure * secs_per_semibreve)
        secs_per_semibreve = (next_measure_t - t) / semibreves_in_measure

        # Add section
        if "marker" in measure and not is_repeat:
            section_name = measure["marker"]["text"]
            fp_sections.append({
                "name": section_name,
                "number": len(fp_sections) + 1,
                "time": t,
            })
            section_info.append(SectionInfo(
                time=t,
                name=section_name,
                measure_index=measure_num,
            ))

        # Add beat lines
        fp_beats.append({
            "time": t,
            "measure": measure_num + 1,
        })
        for i in range(1, int(semibreves_in_measure * 4)):
            fp_beats.append({
                "time": t + (i * secs_per_semibreve / 4),
                "measure": -1,
            })

        measure_info.append(MeasureInfo(
            time=t,
            secs_per_semibreve=secs_per_semibreve,
            note_index=len(fp_notes),
            chord_index=len(fp_chords),
        ))

        # Process notes
        for beat in beats:
            duration_semibreves = (beat["duration"][0] / beat["duration"][1]) if "duration" in beat else 0

            palm_mute = beat.get("palmMute", False)
            tremelo = beat.get("tremolo", False)
            tap = beat.get("tapping", False)

            pick_dir = -1
            if "pickStroke" in beat:
                if beat["pickStroke"] == "down":
                    pick_dir = 0
                elif beat["pickStroke"] == "up":
                    pick_dir = 1

            simultaneous_notes = []
            for note in beat["notes"]:
                # Skip rests and unpitched notes
                if ("rest" in note) or ("fret" not in note):
                    continue

                # Skip ties (but update the sustain duration)
                string = len(track.tuning.strings) - note["string"] - 1
                if "tie" in note:
                    prev_notes[string]["sus"] += duration_semibreves * secs_per_semibreve
                    continue

                string_mute = note.get("dead", False)
                fret = note["fret"] if not string_mute else 0
                hopo_delta = fret - hopo_from.get(string, fret)

                # Set previous note's slide to this note's fret
                if string in slides:
                    prev_notes[string]["sl"] = fret
                    slides.remove(string)

                simultaneous_notes.append({
                    "s": string, # String number
                    "f": fret, # Fret number
                    "sus": duration_semibreves * secs_per_semibreve, # Sustain in seconds
                    "spsb": secs_per_semibreve, # [Internal] seconds per semibreve
                    "sl": -1, # Pitched slide to fret (filled in later)
                    "slu": fret - CONFIG_UNPITCHED_SLIDE_WIDTH if note.get("slide") == "downwards" else -1, # Unpitched slide to fret
                    "bn": (note["bend"]["tone"] / 50) if note.get("bend") else 0, # Bend amount in semitones
                    "ho": hopo_delta > 0, # Hammer-on
                    "po": hopo_delta < 0, # Pull-off
                    "hm": note.get("harmonic") == "natural", # Natural harmonic
                    "hp": note.get("harmonic") in ("pinch", "artificial"), # Pinch harmonic
                    "pm": palm_mute, # Palm mute
                    "mt": string_mute, # String mute
                    "vb": note.get("vibrato", False), # Vibrato
                    "tr": tremelo, # Tremolo
                    "ac": note.get("accentuated", False) or note.get("staccato", False), # Accent (also do staccato)
                    "pkd": pick_dir, # Pick direction
                    "tap": tap, # Tap
                })

                # Hopo handled on the next note
                if string in hopo_from:
                    del hopo_from[string]
                if note.get("hp", False):
                    hopo_from[string] = fret

                # Work out slide destination later
                if note.get("slide") in ("legato", "shift"):
                    slides.add(string)

                prev_notes[string] = simultaneous_notes[-1]

            if len(simultaneous_notes) == 1:
                simultaneous_notes[0]["t"] = t
                fp_notes.append(simultaneous_notes[0])
            elif len(simultaneous_notes) > 1:
                fp_chords.append({
                    "t": t,
                    "id": 0,
                    "hd": False,
                    "notes": simultaneous_notes,
                })

            # Update time
            t += beat["duration"][0] / beat["duration"][1] * secs_per_semibreve

    # Post-process sustains
    for note in fp_notes + [n for chord in fp_chords for n in chord["notes"]]:
        secs_per_beat = note["spsb"] / 4
        # Do not sustain if duration is less than a beat.
        # Slides/bends/vibrato/tremolo are always sustains.
        if (note["sus"] <= secs_per_beat
                and note["sl"] == -1
                and note["slu"] == -1
                and note["bn"] == 0
                and not note["vb"]
                and not note["tr"]):
            note["sus"] = 0
        # Visually shorten the sustain slightly unless it is a slide
        elif (note["sl"] == -1 and note["slu"] == -1):
            note["sus"] -= secs_per_beat * CONFIG_SUSTAIN_MARGIN_BEATS
        del note["spsb"]

    # Add default sections if there are no sections
    if not fp_sections:
        for i, measure_info_item in list(enumerate(measure_info))[::CONFIG_DEFAULT_SECTION_MEASURES]:
            fp_sections.append({
                "name": "Section",
                "number": len(fp_sections) + 1,
                "time": measure_info_item.time,
            })
            section_info.append(SectionInfo(
                time=measure_info_item.time,
                name="Section",
                measure_index=i,
            ))

    # Add an intro section if the first section does not start at time 0
    if fp_sections[0]["time"] > 0:
        fp_sections.insert(0, {
            "name": "Intro",
            "number": 0,
            "time": 0,
        })
        section_info.insert(0, SectionInfo(
            time=0,
            name="Intro",
            measure_index=0,
        ))

    arrangement = {
        "name": track.get_name(),
        "tuning": build_feedpak_tuning(track.tuning, track.instrument & Instrument.BASS),
        "capo": track.capo,
        "notes": fp_notes,
        "chords": fp_chords,
        "handshapes": [],
        "templates": [],
    }
    generate_feedpak_arrangement_anchors(arrangement)

    song_timeline = {
        "beats": fp_beats,
        "sections": fp_sections,
    }

    return arrangement, song_timeline, measure_info, section_info

def _iterate_notes(arrangement: dict) -> Generator[tuple[list[dict], float], None, None]:
    """
    Iterate over notes and chords in an arrangement in chronological order.
    Yields a tuple of (list of notes, time).
    """
    note_index = 0
    chord_index = 0
    notes = arrangement["notes"]
    chords = arrangement["chords"]
    while note_index < len(notes) and chord_index < len(chords):
        note = notes[note_index]
        chord = chords[chord_index]
        if note["t"] < chord["t"]:
            yield [note], note["t"]
            note_index += 1
        else:
            yield chord["notes"], chord["t"]
            chord_index += 1
    while note_index < len(notes):
        yield [notes[note_index]], notes[note_index]["t"]
        note_index += 1
    while chord_index < len(chords):
        yield chords[chord_index]["notes"], chords[chord_index]["t"]
        chord_index += 1

def generate_feedpak_arrangement_anchors(arrangement: dict):
    """
    Generate anchors for a feedpak arrangement.
    The parameter is modified in place.
    """
    anchors = []
    anchor_min_fret = -1
    anchor_max_fret = -1
    for note_group, t in _iterate_notes(arrangement):
        non_open_notes = [note for note in note_group if note["f"]]
        if non_open_notes:
            req_anchor_min_fret = min(note["f"] for note in non_open_notes)
            req_anchor_max_fret = max(note["f"] for note in non_open_notes)
            if not (anchor_min_fret <= req_anchor_min_fret <= req_anchor_max_fret <= anchor_max_fret):
                anchor_min_fret = req_anchor_min_fret
                anchor_max_fret = max(req_anchor_max_fret, req_anchor_min_fret + CONFIG_ANCHOR_MIN_WIDTH - 1)
                anchors.append({
                    "time": t - 0.001, # Slightly before to prevent visual glitches on open notes
                    "fret": anchor_min_fret,
                    "width": anchor_max_fret - anchor_min_fret + 1,
                })
    arrangement["anchors"] = anchors

def _get_title_retune_suffix(mp3: Mp3) -> str:
    if mp3.retuned_by_cents == 0:
        return ""

    semitones, cents = _cents_improper_to_mixed(mp3.retuned_by_cents)
    if semitones == 0:
        return f" ({_sign_string(cents)}c)"

    steps_str = "step" if abs(semitones) == 1 else "steps"
    if cents == 0:
        return f" ({_sign_string(semitones)} {steps_str})"
    else:
        return f" ({_sign_string(semitones)} {steps_str}, {_sign_string(cents)}c)"

def build_feedpak_manifest(song: SongsterrSong, mp3: Mp3) -> dict:
    def get_instrument_type(instrument: int) -> Optional[str]:
        if instrument & Instrument.GUITAR:
            return "guitar"
        elif instrument & Instrument.BASS:
            return "bass"
        return None

    manifest = {
        "feedpak_version": "1.0.0",
        "title": song.title + _get_title_retune_suffix(mp3),
        "artist": song.artist,
        "duration": mp3.duration,
        "song_timeline": _get_song_timeline_filename(),
        "arrangements": [
            {
                "id": f"{track.track_id} - {_to_valid_filename(track.get_name())}",
                "name": track.get_name(),
                "file": _get_arrangement_filename(track),
                "type": get_instrument_type(track.instrument),
                "tuning": build_feedpak_tuning(track.tuning, track.instrument & Instrument.BASS),
                "capo": track.capo,
                "centOffset": track.cent_offset + (-1200 if track.instrument & Instrument.BASS else 0),
            } for track in song.tracks
        ],
        "stems": [{
            "id": "full",
            "file": _get_stem_filename(song),
            "default": True,
        }],
    }
    if mp3.thumbnail:
        manifest["cover"] = _get_cover_filename()
    if mp3.preview:
        manifest["preview"] = _get_preview_filename()
    return manifest

def build_zip(files: dict[str, Union[str, bytes]]) -> bytes:
    """
    Build a zip file from a dictionary of files.
    Binary files are stored without compression.
    """
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for filename, data in files.items():
            if isinstance(data, bytes):
                # MP3 files are already compressed so don't compress them again
                zip_file.writestr(filename, data, compress_type=zipfile.ZIP_STORED)
            else:
                zip_file.writestr(filename, data)
    return zip_buffer.getvalue()

class _ArrangementInfo:
    def __init__(self, arrangement: dict, measure_info: list[MeasureInfo], section_info: list[SectionInfo], instrument: int):
        self.arrangement = arrangement
        self.measure_info = measure_info
        self.section_info = section_info
        self.instrument = instrument

    def get_measure_note_count(self, measure_number: int) -> int:
        if measure_number + 1 < len(self.measure_info):
            end = self.measure_info[measure_number + 1].note_index
        else:
            end = len(self.arrangement["notes"])
        return end - self.measure_info[measure_number].note_index

    def get_measure_chord_count(self, measure_number: int) -> int:
        if measure_number + 1 < len(self.measure_info):
            end = self.measure_info[measure_number + 1].chord_index
        else:
            end = len(self.arrangement["chords"])
        return end - self.measure_info[measure_number].chord_index

    def get_measure_note_and_chord_count(self, measure_number: int) -> int:
        return self.get_measure_note_count(measure_number) + self.get_measure_chord_count(measure_number)

    def get_section_note_and_chord_count(self, section_number: int) -> int:
        if section_number + 1 < len(self.section_info):
            end = self.section_info[section_number + 1].measure_index
        else:
            end = len(self.measure_info)

        total = 0
        for i in range(self.section_info[section_number].measure_index, end):
            total += self.get_measure_note_and_chord_count(i)
        return total

    def get_section_measure_count(self, section_number: int) -> int:
        if section_number + 1 < len(self.section_info):
            end = self.section_info[section_number + 1].measure_index
        else:
            end = len(self.measure_info)
        return end - self.section_info[section_number].measure_index

def build_feedpak(song: SongsterrSong,
                  mp3: Mp3,
                  substitute_empty_sections: bool=False,
                  json_indent: Optional[int]=None,
                  manifest_extra: Optional[dict[str, object]]=None) -> dict[str, Union[str, bytes]]:
    files = {}
    manifest = build_feedpak_manifest(song, mp3)
    if manifest_extra:
        manifest.update(manifest_extra)
    files["manifest.yaml"] = yaml.dump(manifest)

    arrangements = []
    song_timeline = None
    for i, track in enumerate(song.tracks):
        arrangement, cur_song_timeline, measure_info, section_info = build_feedpak_arrangement(song, track)
        arrangements.append(_ArrangementInfo(arrangement,
                                             measure_info,
                                             section_info,
                                             track.instrument))
        if i == 0:
            song_timeline = cur_song_timeline
    files[_get_song_timeline_filename()] = json.dumps(song_timeline, indent=json_indent)

    if substitute_empty_sections:
        for section_number in reversed(range(len(arrangements[0].section_info))):
            for arrangement in arrangements:
                # Check if the section is empty.
                # If there are only a few notes at the start or end of the section,
                # then we can exclude those areas and consider the rest empty.
                # The empty section must be at least 2 * CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES long.
                section_info = arrangement.section_info[section_number]
                measure_count = arrangement.get_section_measure_count(section_number)
                if measure_count < CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES:
                    continue
                start_has_notes = 0
                for i in range(CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES):
                    if arrangement.get_measure_note_and_chord_count(section_info.measure_index + i) > 0:
                        start_has_notes = 1
                        break
                end_has_notes = 0
                for i in range(CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES):
                    if arrangement.get_measure_note_and_chord_count(section_info.measure_index + measure_count - 1 - i) > 0:
                        end_has_notes = 1
                        break
                empty_measure_count = measure_count - (CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES * (start_has_notes + end_has_notes))
                if empty_measure_count < 2 * CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES:
                    continue
                has_notes = False
                for i in range(empty_measure_count):
                    n = section_info.measure_index + (CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES * start_has_notes) + i
                    if arrangement.get_measure_note_and_chord_count(n) > 0:
                        has_notes = True
                        break
                if has_notes:
                    continue

                # Find suitable substitute arrangement
                def create_find_active_fn(instrument: int) -> Callable[[_ArrangementInfo], bool]:
                    return (lambda a: (a.instrument & instrument)
                                  and a.arrangement["tuning"] == arrangement.arrangement["tuning"]
                                  and a.arrangement["capo"] == arrangement.arrangement["capo"]
                                  and a.get_section_note_and_chord_count(section_number) > 0)
                sorted_arrangements = sorted(arrangements, key=lambda a: a.get_section_note_and_chord_count(section_number), reverse=True)
                active = _list_find(sorted_arrangements, create_find_active_fn(arrangement.instrument))
                if not active and (arrangement.instrument & Instrument.GUITAR):
                    active = _list_find(sorted_arrangements, create_find_active_fn(Instrument.GUITAR))
                if not active:
                    continue

                # Insert notes from the active arrangement
                copy_measure_index = section_info.measure_index + (CONFIG_SUBSTITUTE_EMPTY_SECTION_MARGIN_MEASURES * start_has_notes)
                notes_dst_index = arrangement.measure_info[copy_measure_index].note_index
                notes_src_index = active.measure_info[copy_measure_index].note_index
                notes_src_count = sum(active.get_measure_note_count(copy_measure_index + i) for i in range(empty_measure_count))
                arrangement.arrangement["notes"][notes_dst_index:notes_dst_index] = active.arrangement["notes"][notes_src_index:notes_src_index + notes_src_count]

                chords_dst_index = arrangement.measure_info[copy_measure_index].chord_index
                chords_src_index = active.measure_info[copy_measure_index].chord_index
                chords_src_count = sum(active.get_measure_chord_count(copy_measure_index + i) for i in range(empty_measure_count))
                arrangement.arrangement["chords"][chords_dst_index:chords_dst_index] = active.arrangement["chords"][chords_src_index:chords_src_index + chords_src_count]

        for arrangement in arrangements:
            generate_feedpak_arrangement_anchors(arrangement.arrangement)

    for track, arrangement in zip(song.tracks, arrangements):
        files[_get_arrangement_filename(track)] = json.dumps(arrangement.arrangement, indent=json_indent)

    files[_get_stem_filename(song)] = mp3.data
    if mp3.thumbnail:
        files[_get_cover_filename()] = mp3.thumbnail
    if mp3.preview:
        files[_get_preview_filename()] = mp3.preview
    return files

async def download_feedpak(song_id: int,
                           include_thumbnail: bool=False,
                           include_preview: bool=False,
                           retune_by_cents: int=0,
                           zero_cents: bool=False,
                           substitute_empty_sections: bool=False,
                           video_type: str=VideoType.MAIN,
                           track_index_for_video_type: int=0,
                           title_override: Optional[str]=None,
                           json_indent: Optional[int]=None,
                           manifest_extra: Optional[dict[str, object]]=None,
                           mock_mp3_path: Optional[str]=None) -> tuple[SongsterrSong, Mp3, dict[str, Union[str, bytes]]]:
    """
    Download a Songsterr song, corresponding MP3 from YouTube, and create a feedpak.
    Returns a dictionary of files that can be used to construct a zip file or directory.
    Also returns the intermediate SongsterrSong and MP3 in case extra metadata is needed.
    """
    exc = None
    songs = await download_songsterr_song(song_id)
    first_attempt = True
    for song in songs:
        if (video_type == VideoType.MAIN and song.yt_video_type in (VideoType.MAIN, VideoType.MAIN_ALT)):
            pass
        elif track_index_for_video_type >= len(song.tracks):
            continue
        elif video_type != song.yt_video_type or song.tracks[track_index_for_video_type].track_hash not in song.compatible_track_hashes:
            continue

        if not first_attempt:
            print(f"Trying next youtube video")
        first_attempt = False

        if title_override is not None:
            song = song._replace(title=title_override)

        # Keep only guitar and bass tracks
        song = song._replace(tracks=[
            track for track in song.tracks
            if ((track.instrument & Instrument.GUITAR) or (track.instrument & Instrument.BASS)) and track.tuning
        ])

        # Retune tracks
        cent_offsets = []
        for j, track in enumerate(song.tracks):
            cent_offsets.append(track.cent_offset)
            retune_semitones, retune_cents = _cents_improper_to_mixed(retune_by_cents + track.cent_offset)
            new_track = track._replace(
                tuning=track.tuning.add_semitones(retune_semitones),
                cent_offset=retune_cents,
            )
            song.tracks[j] = new_track
        if zero_cents:
            retune_by_cents -= _mode(cent_offsets)

        # Download MP3
        try:
            mp3 = await download_youtube_mp3(song.yt_video_id,
                                             include_thumbnail=include_thumbnail,
                                             include_preview=include_preview,
                                             retune_by_cents=retune_by_cents,
                                             mock_mp3_path=mock_mp3_path)
        except YoutubeDownloadError as e:
            if not exc:
                exc = e
            continue

        # Build feedpak
        feedpak = build_feedpak(song,
                                mp3,
                                substitute_empty_sections=substitute_empty_sections,
                                json_indent=json_indent,
                                manifest_extra=manifest_extra)
        return song, mp3, feedpak

    raise exc or ValueError("No valid tracks found for this song")

################################################################
# CLI app
################################################################

async def _handle_search(args: argparse.Namespace) -> list[SongsterrSongSearchResult]:
    query = " ".join(args.query)
    print(f"==== SEARCH {query} ====")
    results = await search_songsterr(query)
    print(f"Search results for '{query}':")
    for result in results:
        print(f"  - {result.title} by {result.artist} (ID: {result.song_id})")
        for track in result.tracks:
            tuning_name = f" ({track.tuning.name})" if track.tuning else ""
            print(f"    - {track.get_name()}{tuning_name}")
    return results

async def _handle_download_by_id(args: argparse.Namespace):
    print(f"==== DOWNLOAD SONG {args.song_id} ====")

    # Need ffmpeg for youtube download and audio processing
    if not has_ffmpeg():
        raise RuntimeError("ffmpeg is not installed")
    if (args.retune_by or args.zero_cents) and not has_ffmpeg_rubberband_filter():
        raise RuntimeError("Retuning requires ffmpeg with the rubberband filter installed")

    if args.video_type != "main" and args.track_index is None:
        raise ValueError("Must specify --track-index when using --video-type other than 'main'")
    elif args.video_type == "main" and args.track_index is not None:
        raise ValueError("Cannot specify --track-index when using --video-type 'main'")

    video_type = VideoType.MAIN
    if args.video_type == "backing":
        video_type = VideoType.BACKING
    elif args.video_type == "solo":
        video_type = VideoType.SOLO
    elif args.video_type == "playthrough":
        video_type = VideoType.PLAYTHROUGH

    # Add custom metadata for debugging and tracing purposes
    cmdline = dict(vars(args))
    cmdline.pop("command", None)
    cmdline.pop("query", None)
    cmdline.pop("output", None)
    cmdline.pop("artist_folder", None)
    cmdline.pop("remove_existing", None)
    cmdline.pop("input_file", None)
    cmdline.pop("worker_count", None)
    cmdline.pop("mp3", None)
    cmdline.pop("log", None)
    manifest_extra = {
        "songsterr_to_feedpak_version": CONFIG_SONGSTERR_TO_FEEDPAK_VERSION,
        "songsterr_to_feedpak_cmdline": cmdline,
        "songsterr_to_feedpak_credits": "Created with https://github.com/kevlu123/songsterr_to_feedpak",
    }

    song, mp3, feedpak = await download_feedpak(args.song_id,
                                               include_thumbnail=args.thumbnail,
                                               include_preview=args.preview,
                                               retune_by_cents=args.retune_by,
                                               zero_cents=args.zero_cents,
                                               substitute_empty_sections=args.substitute_empty_sections,
                                               video_type=video_type,
                                               track_index_for_video_type=args.track_index or 0,
                                               title_override=args.title,
                                               json_indent=args.json_indent,
                                               manifest_extra=manifest_extra,
                                               mock_mp3_path=args.mp3)

    artist_dir = _to_valid_filename(song.artist) if args.artist_folder else "."
    default_filename = _to_valid_filename(f"{song.artist} - {song.title} - {song.song_id}{_get_title_retune_suffix(mp3)}.feedpak")
    if args.output:
        if os.path.isdir(args.output):
            feedpak_dst = os.path.join(args.output, artist_dir, default_filename)
        else:
            feedpak_dst = args.output
    else:
        feedpak_dst = os.path.join(artist_dir, default_filename)

    if args.remove_existing:
        if os.path.exists(feedpak_dst):
            if os.path.isdir(feedpak_dst):
                shutil.rmtree(feedpak_dst)
            else:
                os.remove(feedpak_dst)

    if args.artist_folder:
        os.makedirs(os.path.dirname(feedpak_dst), exist_ok=True)

    if args.folder:
        for filename, data in feedpak.items():
            full_path = os.path.join(feedpak_dst, filename)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(data if isinstance(data, bytes) else data.encode('utf-8'))
    else:
        with open(feedpak_dst, 'wb') as f:
            f.write(build_zip(feedpak))
    print(f"==== PACKAGED {song.artist} - {song.title} ====")

async def _handle_download(args: argparse.Namespace):
    results = await _handle_search(args)
    args.song_id = results[0].song_id
    await _handle_download_by_id(args)

async def _run_parallel_download(work: list[Callable[[], Awaitable[None]]],
                                 worker_count: int) -> list:
    """
    Execute a list of async work while limiting the number of concurrent workers.
    Takes a list of tuples of (async function factory, name) and a worker count.
    The async function factory, when called, returns an awaitable.
    """
    semaphore = asyncio.Semaphore(worker_count)

    async def do_work(index: int, fn: Callable[[], object]):
        async with semaphore:
            # Stagger work to mitigate rate limiting errors
            if index < worker_count:
                await asyncio.sleep(index)
            await fn()

    tasks = [asyncio.create_task(do_work(i, w)) for i, w in enumerate(work)]
    await asyncio.gather(*tasks)

async def _handle_download_list_by_id(args: argparse.Namespace):
    with open(args.input_file, 'r') as f:
        song_ids = [int(line.strip()) for line in f if line.strip()]

    with (open(args.log, 'w') if args.log else contextlib.nullcontext()) as log_file:
        work = []
        for song_id in song_ids:
            args_copy = copy.deepcopy(args)
            args_copy.song_id = song_id

            async def work_fn(a=args_copy):
                try:
                    await _handle_download_by_id(a)
                except Exception as e:
                    print(f"Download {a.song_id} failed: {e}")
                    traceback.print_exc()
                    if log_file:
                        log_file.write(f"{a.song_id}: failed - {e}\n")
                        log_file.write(traceback.format_exc() + "\n")
                        log_file.flush()
                else:
                    if log_file:
                        log_file.write(f"{a.song_id}: success\n")
                        log_file.flush()

            work.append(work_fn)
        await _run_parallel_download(work, args.worker_count)

async def _handle_download_list(args: argparse.Namespace):
    with open(args.input_file, 'r') as f:
        queries = [line.strip() for line in f if line.strip()]

    with (open(args.log, 'w') if args.log else contextlib.nullcontext()) as log_file:
        work = []
        for query in queries:
            args_copy = copy.deepcopy(args)
            args_copy.query = [query]

            async def work_fn(a=args_copy):
                try:
                    await _handle_download(a)
                except Exception as e:
                    print(f"Download {a.query[0]} failed: {e}")
                    traceback.print_exc()
                    if log_file:
                        log_file.write(f"{a.query[0]}: failed - {e}\n")
                        log_file.write(traceback.format_exc() + "\n")
                        log_file.flush()
                else:
                    if log_file:
                        log_file.write(f"{a.query[0]}: success\n")
                        log_file.flush()

            work.append(work_fn)
        await _run_parallel_download(work, args.worker_count)

async def main():
    def add_search_args(subparser: argparse.ArgumentParser):
        subparser.add_argument("query", nargs=argparse.ONE_OR_MORE, type=str, help="Search query.")

    def add_download_args(subparser: argparse.ArgumentParser, multidownload: bool):
        subparser.add_argument("-o", "--output", type=str, metavar="PATH", help=
                               "The output feedpak path.\n"
                               "If this refers to an existing folder, the feedpak will be placed in that folder.\n"
                               "Otherwise, this will be used as the filename of the feedpak.\n\n")

        subparser.add_argument("-a", "--artist-folder", action="store_true", help=
                               "Create a folder for the artist and place the feedpak inside it.\n"
                               "This option is recommended to keep your files organised.\n\n")

        subparser.add_argument("-s", "--substitute-empty-sections", action="store_true", help=
                               "Substitute empty sections with notes from another track.\n"
                               "This option is recommended because many Songsterr tracks have sections\n"
                               "with no notes once the instrument or effect is changed. To fill in these\n"
                               "sections, notes from another track with the same tuning and capo are used.\n"
                               "The most similar track is chosen (catagorised by rhythm, lead, bass)\n\n")

        subparser.add_argument("-t", "--thumbnail", action="store_true", help=
                               "Include the YouTube thumbnail as the cover image in the feedpak.\n"
                               "Note that FeedBack already has a built-in option to fetch a thumbnail\n"
                               "which will likely give better results than this option.\n\n")

        subparser.add_argument("-p", "--preview", action="store_true", help=
                               "Include a preview audio clip in the feedpak.\n"
                               "Note that FeedBack already has a built-in option to generate a preview\n"
                               "which will likely give better results than this option.\n\n")

        subparser.add_argument("-r", "--retune-by", metavar="CENTS", type=int, default=0, help=
                                  "Change the audio pitch by the given number of cents (1 semitone=100 cents).\n\n")

        subparser.add_argument("-z", "--zero-cents", action="store_true", help=
                                  "Zero out the cent offset i.e. retune to A440.\n"
                                  "This is heuristically detected.\n\n")

        if multidownload:
            subparser.set_defaults(title=None,
                                   video_type="main",
                                   track_index=None)

            subparser.add_argument("-l", "--log", type=str, help=
                                   "Path to a log file to write the success status of each download.\n\n")
        else:
            subparser.add_argument("-T", "--title", type=str, help=
                                   "Override the song title.\n\n")

            video_types = ["main", "backing", "solo", "playthrough"]
            subparser.add_argument("-V", "--video-type", choices=video_types, default="main", help=
                                   "Type of YouTube video to download (default: main).\n"
                                   "Must be used with --track-index when video type is not main.\n\n")

            subparser.add_argument("-I", "--track-index", type=int, help=
                                   "When downloading a video type other than main, specifies the\n"
                                   "0-based track index (as read from top to bottom on the website)\n"
                                   "to use for the backing/solo/playthrough video. This is required\n"
                                   "because each track has different backing/solo/playthrough videos.\n"
                                   "This must be used with --video-type.\n\n")

        subparser.add_argument("-f", "--folder", action="store_true", help=
                               "Save the feedpak as a folder instead of a single file.\n"
                               "This is useful for development and testing purposes.\n\n")

        subparser.add_argument("-R", "--remove-existing", action="store_true", help=
                               "Delete the existing file or folder at the destination path before creating the feedpak.\n"
                               "When writing a file (no -f) to a location where a file already exists, the existing file\n"
                               "will be overwritten even without this option. This option is intended for switching between\n"
                               "the file and folder formats and for cleaning out old folders.\n\n")

        subparser.add_argument("-j", "--json-indent", type=int, help=
                               "The number of spaces to use for indentation when serialising to JSON (default: None).\n"
                               "Useful for readability during debugging.\n\n")

        subparser.add_argument("-m", "--mp3", type=str, help=
                               "Use a locally stored MP3 file to mock downloading from YouTube.\n"
                               "This is intended for development purposes to prevent being flagged by YouTube for\n"
                               "constantly downloading videos.\n\n")

    def add_download_list_args(subparser: argparse.ArgumentParser):
        subparser.add_argument("-w", "--worker-count", type=int, default=10, help=
                               "The number of parallel workers to use for downloading (default: 10).\n\n")

    parser = argparse.ArgumentParser(
        description="Songsterr to Feedpak Converter",
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-v", "--version", action="version", version=f"%(prog)s v{CONFIG_SONGSTERR_TO_FEEDPAK_VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_search = subparsers.add_parser("search", formatter_class=argparse.RawTextHelpFormatter, help=
                                          "Search Songsterr and print the song information of the search results.\n\n")
    add_search_args(parser_search)

    parser_download_by_id = subparsers.add_parser("download-by-id", formatter_class=argparse.RawTextHelpFormatter, help=
                                                  "Download and create a feedpak from a Songsterr song ID.\n\n")
    parser_download_by_id.add_argument("song_id", type=int, help="Songsterr song ID.")
    add_download_args(parser_download_by_id, multidownload=False)

    parser_download = subparsers.add_parser("download", formatter_class=argparse.RawTextHelpFormatter, help=
                                            "Search Songsterr, and download and create a feedpak from the first result.\n"
                                            "This is a convenience command that combines the search and download-by-id commands.\n\n")
    add_search_args(parser_download)
    add_download_args(parser_download, multidownload=False)

    parser_download_list_by_id = subparsers.add_parser("download-list-by-id", formatter_class=argparse.RawTextHelpFormatter, help=
                                                       "Download and create feedpaks for all the songs specified in a text file.\n"
                                                       "The text file should contain one song ID per line.\n\n")
    parser_download_list_by_id.add_argument("input_file", type=str, help="Path to the text file containing song IDs.")
    add_download_list_args(parser_download_list_by_id)
    add_download_args(parser_download_list_by_id, multidownload=True)

    parser_download_list = subparsers.add_parser("download-list", formatter_class=argparse.RawTextHelpFormatter, help=
                                                 "Search Songsterr, and download and create feedpaks for all the songs specified in a text file.\n"
                                                 "The text file should contain one search query per line.\n")
    parser_download_list.add_argument("input_file", type=str, help="Path to the text file containing search queries.")
    add_download_list_args(parser_download_list)
    add_download_args(parser_download_list, multidownload=True)

    args = parser.parse_args()

    print(f"==== songsterr_to_feedpak v{CONFIG_SONGSTERR_TO_FEEDPAK_VERSION}====")
    if args.command == "search":
        await _handle_search(args)
    elif args.command == "download-by-id":
        await _handle_download_by_id(args)
    elif args.command == "download":
        await _handle_download(args)
    elif args.command == "download-list-by-id":
        await _handle_download_list_by_id(args)
    elif args.command == "download-list":
        await _handle_download_list(args)
    print("==== DONE ====")

if __name__ == "__main__":
    asyncio.run(main())
