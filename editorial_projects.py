"""Proyectos derivados sin inferencia; tiempos locales y procedencia por segmentos."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import editorial_chunks
import editorial_master
import medios
from editorial_io import atomic_write_json, read_json, validate_range


def time_map(segments, duration):
    result, cursor, previous = [], 0.0, -1.0
    for start, end in segments:
        start, end = validate_range(start, end, duration, label="segmento fuente")
        if start < previous:
            raise ValueError("segmentos fuente desordenados o solapados")
        result.append(dict(source_ini=start, source_fin=end,
                           child_ini=round(cursor, 6), child_fin=round(cursor + end - start, 6)))
        cursor += end - start
        previous = end
    if not result:
        raise ValueError("un hijo necesita segmentos conservados")
    return result


def map_range(start, end, mapping):
    """Intersecciones con trazabilidad, también si un evento cruza varios recortes."""
    result = []
    for index, segment in enumerate(mapping):
        a, b = max(start, segment["source_ini"]), min(end, segment["source_fin"])
        if b > a:
            result.append(dict(t_ini=round(segment["child_ini"] + a - segment["source_ini"], 6),
                               t_fin=round(segment["child_ini"] + b - segment["source_ini"], 6),
                               source_ini=a, source_fin=b, segment=index))
    return result


def _slice(events, mapping, id_key):
    result = []
    for index, event in enumerate(events):
        source_id = event.get(id_key, f"event-{index + 1}")
        for part in map_range(event["t_ini"], event["t_fin"], mapping):
            item = copy.deepcopy(event)
            item.update(t_ini=part["t_ini"], t_fin=part["t_fin"],
                        source_id=source_id, source_range=[part["source_ini"], part["source_fin"]],
                        source_segment=part["segment"])
            item[id_key] = f"{source_id}~s{part['segment'] + 1}"
            result.append(item)
    return sorted(result, key=lambda e: (e["t_ini"], e["t_fin"], e[id_key]))


def derive_master(parent, child_info, child_fp, segments, *, name, parent_path=None):
    mapping = time_map(segments, parent["media"]["duration"])
    tracks = {}
    for position, (track_id, original) in enumerate(parent["tracks"].items()):
        track = {key: copy.deepcopy(value) for key, value in original.items()
                 if key not in ("words", "utterances", "laughter", "arousal", "emotions", "intensity")}
        track.setdefault("stream_index", position)
        stream = next((p for p in child_info["pistas"] if p["idx"] == track["stream_index"]), None)
        if stream is None:
            raise ValueError(f"el hijo no conserva la pista {track_id}")
        track["offset"] = stream["delta"]
        for collection in ("words", "laughter", "arousal", "emotions", "intensity"):
            track[collection] = _slice(original.get(collection, []), mapping,
                                        "word_id" if collection == "words" else "event_id")
        words = track["words"]
        originals = original.get("utterances", [u for u in parent["conversation"]["utterances"]
                                                if u["track_id"] == track_id])
        source_utterances = {u["utterance_id"]: u for u in originals}
        words_by_source = {}
        for word in words:
            words_by_source.setdefault((word["source_id"], word["source_segment"]), []).append(word)
        track["utterances"] = []
        for utterance in _slice(originals, mapping, "utterance_id"):
            source = source_utterances[utterance["source_id"]]
            ids = set(source.get("word_ids", []))
            selected = sorted((w for identifier in ids for w in words_by_source.get(
                (identifier, utterance["source_segment"]), [])
                if w["t_ini"] < utterance["t_fin"] and w["t_fin"] > utterance["t_ini"]),
                key=lambda w: (w["t_ini"], w["word_id"]))
            # Sin palabras no es posible atribuir texto parcial de forma fiable.
            if not selected:
                continue
            utterance.update(word_ids=[w["word_id"] for w in selected],
                             text=" ".join(w["text"] for w in selected),
                             t_ini=min(w["t_ini"] for w in selected),
                             t_fin=max(w["t_fin"] for w in selected))
            for key in ("overlap_group", "duplicate_group", "duplicate_secondary"):
                utterance.pop(key, None)
            laughs = [e for e in track["laughter"] if e["t_ini"] < utterance["t_fin"]
                      and e["t_fin"] > utterance["t_ini"]]
            arousals = [e for e in track["arousal"] if e["t_ini"] < utterance["t_fin"]
                        and e["t_fin"] > utterance["t_ini"]]
            utterance["signals"] = {
                "laughter_event_ids": [e["event_id"] for e in laughs],
                "laughter_max": max((e.get("max_conf", e.get("conf", 0)) for e in laughs), default=None),
                "emphasis_max": max((w["emphasis_score"] for w in selected
                                      if w.get("emphasis_score") is not None), default=None),
                **{key + "_mean": editorial_master._event_average(
                    arousals, utterance["t_ini"], utterance["t_fin"], key)
                   for key in ("arousal_z", "valence", "dominance")},
                "intensity_z_mean": editorial_master._event_average(
                    selected, utterance["t_ini"], utterance["t_fin"], "intensity_z")}
            track["utterances"].append(utterance)
        tracks[track_id] = track
    utterances = sorted((u for t in tracks.values() for u in t["utterances"]),
                        key=lambda u: (u["t_ini"], u["track_id"], u["t_fin"]))
    words_by_id = {w["word_id"]: w for t in tracks.values() for w in t["words"]}
    overlaps = editorial_master._overlap_groups(utterances)
    duplicates = editorial_master._deduplicate_bleed(utterances, words_by_id)
    return {"schema": "editorial-master/1", "project": {"name": name, "profile": "editorial_voz"},
            "media": {"path": child_info["path"], "duration": child_info["duracion"],
                      "t0": child_info["t0"], "fingerprint": child_fp},
            "transcription": copy.deepcopy(parent.get("transcription", {})), "tracks": tracks,
            "conversation": {"utterances": utterances,
                             "clean_utterance_ids": [u["utterance_id"] for u in utterances
                                                     if not u.get("duplicate_secondary")],
                             "overlap_groups": overlaps, "duplicate_groups": duplicates},
            "chunks": [], "provenance": {"method": "derived/no-inference", "baselines": "parent"},
            "derivation": {"schema": "editorial-derivation/1",
                           "source_master_digest": editorial_chunks.source_master_digest(parent),
                           "source_master": str(parent_path) if parent_path else None,
                           "source_fingerprint": parent["media"]["fingerprint"],
                           "source_range": [mapping[0]["source_ini"], mapping[-1]["source_fin"]],
                           "segments": mapping, "ancestor": copy.deepcopy(parent.get("derivation"))}}


def derive_layers(parent_layers, mapping, *, child_fingerprint, child_digest, parent_digest=None):
    """Capas del padre (`layers/`: temas, pedidos, capas de la AI) remapeadas al reloj
    del hijo (plan-montaje-ai.md §6): cada rango se intersecta con los segmentos
    conservados y se traslada (`map_range`); un rango que cruza un recorte queda en
    dos rangos contiguos; un item cuyos rangos desaparecen por completo no se copia,
    ni sus descendientes. Los `item_id` y `layer_id` se conservan (identidad estable
    entre padre e hijo); cada item lleva `source_item_id` y cada rango `source_range`.
    Estados y `edited` se conservan. Puro."""
    import editorial_trims
    result = []
    for layer in parent_layers or []:
        if layer.get("deleted"):
            continue
        by_id = {i["item_id"]: i for i in layer.get("items") or []}
        derived, dropped = {}, set()
        for item in layer.get("items") or []:
            ranges = []
            for part in item.get("ranges") or []:
                for hit in map_range(float(part["t_ini"]), float(part["t_fin"]), mapping):
                    if hit["t_fin"] - hit["t_ini"] <= 0.0005:
                        continue
                    ranges.append({**{k: v for k, v in part.items() if k not in ("t_ini", "t_fin")},
                                   "t_ini": hit["t_ini"], "t_fin": hit["t_fin"],
                                   "source_range": [hit["source_ini"], hit["source_fin"]]})
            if not ranges:
                dropped.add(item["item_id"])
                continue
            ranges.sort(key=lambda r: (r["t_ini"], r["t_fin"]))
            derived[item["item_id"]] = {**copy.deepcopy(item), "ranges": ranges,
                                        "source_item_id": item["item_id"]}
        # un item sin rangos arrastra a sus descendientes (no pueden quedar huérfanos)
        changed = True
        while changed:
            changed = False
            for identifier, item in list(derived.items()):
                parent_id = item.get("parent_id")
                if parent_id and (parent_id in dropped or parent_id not in derived):
                    dropped.add(identifier)
                    del derived[identifier]
                    changed = True
        items = [derived[i["item_id"]] for i in layer.get("items") or [] if i["item_id"] in derived]
        child = {**copy.deepcopy(layer), "items": items, "revision": 0,
                 "media_fingerprint": editorial_trims.identity(child_fingerprint),
                 "source_master_digest": child_digest,
                 "derived_from": {"source_master_digest": parent_digest,
                                  "layer_revision": int(layer.get("revision", 0)),
                                  "dropped_item_ids": sorted(dropped)}}
        result.append(child)
    return result


def publish_child(root, parent, media_path, segments, *, parent_path=None, final_media=None,
                  layers=None, lane_order=None):
    """Publica el proyecto hijo: master derivado y, si se pasan, las capas del padre
    remapeadas (`layers/<id>.json`) y el orden de carriles (`views/lanes.json`).
    `trims.json` NO se propaga: los recortes ya están aplicados en el hijo."""
    info = medios.inspeccionar(media_path)
    fp = medios.fingerprint(media_path, info)
    master = derive_master(parent, info, fp, segments, name=Path(media_path).stem,
                           parent_path=parent_path)
    # No guardar localizadores efímeros del staging.
    master["media"]["path"] = str(final_media or media_path)
    written = editorial_master.write_package(root, master)["master"]
    if layers:
        import editorial_layers
        derived = derive_layers(layers, master["derivation"]["segments"], child_fingerprint=fp,
                                child_digest=editorial_chunks.source_master_digest(master),
                                parent_digest=master["derivation"]["source_master_digest"])
        for layer in derived:
            editorial_layers.validate_layer(layer, master)
            atomic_write_json(Path(root) / "layers" / (layer["layer_id"] + ".json"), layer)
        if lane_order:
            editorial_layers.save_lane_order(root, lane_order)
    return written


def ensure_track_audio(master_path, source, *, cancel=None):
    """Extracción reanudable de audio del hijo para RMS; jamás ejecuta inferencia."""
    master_path = Path(master_path)
    master = read_json(master_path)
    info = medios.inspeccionar(source)
    import podcast_export
    if not podcast_export.source_matches(master, source, info):
        raise ValueError("el audio no pertenece al proyecto")
    paths = {}
    for track_id, track in master["tracks"].items():
        target = master_path.parent / "tracks" / track_id / "audio.flac"
        checkpoint = target.with_suffix(".manifest.json")
        key = {"fingerprint": {k: master["media"]["fingerprint"].get(k) for k in
                              ("size", "hash_muestreado", "inventario_sha256")},
               "stream_index": track["stream_index"], "schema": "editorial-derived-audio/1"}
        reusable = False
        if target.is_file() and checkpoint.is_file():
            saved = read_json(checkpoint)
            reusable = saved.get("key") == key and saved.get("sha256") == medios.hash_archivo(target)
        if not reusable:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(".audio.partial.flac")
            try:
                stream = next(p for p in info["pistas"] if p["idx"] == track["stream_index"])
                medios.extraer_pista(source, stream, temporary, mono=True, sample_rate=16000, cancel=cancel)
                os.replace(temporary, target)
                atomic_write_json(checkpoint, {"key": key, "sha256": medios.hash_archivo(target)})
            finally:
                temporary.unlink(missing_ok=True)
        paths[track_id] = target
    return paths
