#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nd_monomulti.py — modulo M de A.2 (CAMBIAR ALMACENES EN PEDIDOS): tags MONO / MULTI en pedidos D10.

Regla (decision de Bruno, 05/10/2026):
  * Alcance: pedidos ABIERTOS con tag D10, con al menos una linea por enviar (unfulfilledQuantity > 0),
    SIN ningun tag REVISANDO / REVISION / REVISIÓN y SIN ningun tag que empiece por DESPIECE
    (DESPIECE, DESPIECE-REVISAR, DESPIECE ND-...).
  * MONO  = las lineas por enviar tienen UNA sola referencia (SKU), aunque lleve varias unidades
            (5 unidades de la misma referencia = MONO).
    MULTI = dos o mas referencias (SKU) distintas.  Una linea sin SKU cuenta como una referencia mas ('?').
  * Mantenimiento en cada corrida:
      - falta el tag correcto           -> tagsAdd
      - lleva el tag contrario (o los dos) -> tagsRemove del equivocado, y luego tagsAdd del correcto
      - lleva MONO/MULTI pero ya NO esta en alcance (ya no es D10, tiene revision/despiece, sin lineas)
                                          -> tagsRemove
  * NUNCA tagsAdd y tagsRemove del mismo pedido en la misma mutacion (regla de Shopify del 01/09): los
    lotes de quitar (mm_r_NN) van antes y se verifican; luego los de poner (mm_a_NN).
  * Tope de seguridad: mas de 300 operaciones (poner + quitar) -> no se genera ningun lote (se reporta).
    --tope N lo sube SOLO con autorizacion expresa (carga inicial). --lote N cambia el tamaño de lote (25).

Este script NO habla con Shopify ni lleva credenciales: solo procesa archivos que deja Claude (bulk de Composio).
Comandos:  selftest | preparar | verificar
Salida: 0 ok · 2 ERROR / uso · 3 discrepancias · 4 no releidos · 5 CONTENIDO AJENO (v88, 06/10/2026: la bulk
descargada no es la propia segun la firma de su query, Q_MM en preparar y Q_TAGS en verificar; relanzar ESA bulk UNA vez).
"""
import argparse
import csv
import hashlib
import json
import os
import ssl
import sys
import time
import urllib.request

TAG_MONO = "MONO"
TAG_MULTI = "MULTI"
TAGS_MM = (TAG_MONO, TAG_MULTI)
REVISION_UPPER = {"REVISANDO", "REVISION", "REVISIÓN"}
TOPE_OPS = 300
LOTE_OPS = 25

Q_PEDIDOS = ('{ orders(query: "status:open") { edges { node { id name tags '
             'lineItems { edges { node { id sku unfulfilledQuantity } } } } } } }')


class NDError(Exception):
    pass


class BulkAjena(NDError):
    """v88: la bulk descargada es la de OTRO proceso (la app de Composio es compartida). Exit 5."""


Q_TAGS = '{ orders(query: "status:open") { edges { node { id tags } } } }'  # v88: solo para la firma (verificar)

# v88: firma de cada bulk propia, sacada de su query literal (maestro §2.4): (tipo gid de las filas raiz, claves raiz
# que pide la query, claves raiz obligatorias, tipos gid admitidos en las filas hijas).
FIRMAS = {
    "mm": ("Order", {"id", "name", "tags"}, {"id", "name", "tags"}, {"LineItem"}),  # Q_MM: lineItems -> filas LineItem
    "tags": ("Order", {"id", "tags"}, {"id", "tags"}, set()),                      # Q_TAGS: sin filas hijas
}


def comprobar_firma(path, bulk):
    """v88: no hace nada si `path` es la bulk propia `bulk` (vacia tambien vale); si no, BulkAjena."""
    raiz, piden, oblig, hijos = FIRMAS[bulk]
    with open(path, encoding="utf-8") as f:
        for n, raw in enumerate(f, 1):
            if not raw.strip():
                continue
            try:
                r = json.loads(raw)
            except ValueError:
                r, motivo = None, "JSON roto"
            else:
                motivo = None if isinstance(r, dict) else "no es un objeto"
            if not motivo:
                i = r.get("id")
                tipo = i.split("/")[3] if isinstance(i, str) and i.startswith("gid://shopify/") else ""
                if "__parentId" not in r:
                    falta, sobra = sorted(oblig - set(r)), sorted(set(r) - piden)
                    if tipo != raiz or falta or sobra:
                        motivo = f"id {str(i)[:60]}, falta {falta}, sobra {sobra} (se espera raiz {raiz})"
                elif tipo not in hijos:
                    motivo = f"id {str(i)[:60]}, hija de un tipo que la query no pide (se esperan {sorted(hijos) or 'ninguna'})"
            if motivo:
                raise BulkAjena(f"CONTENIDO AJENO ({bulk}): línea {n}, {motivo}. Probablemente es la bulk de OTRO proceso: "
                                f"NO seguir con ella; chequear currentBulkOperation y relanzar ESA bulk UNA vez (maestro §2.3).")


def _ssl_context():
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if cafile and os.path.exists(cafile):
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def contar_lineas(path):
    n = 0
    with open(path, "rb") as f:
        for linea in f:
            if linea.strip():
                n += 1
    return n


def validar_descarga(path, object_count, file_size, firma=None):
    esperado = int(object_count or 0)
    lineas = contar_lineas(path) if os.path.exists(path) else 0
    if lineas != esperado:
        raise NDError(f"Descarga {path}: {lineas} lineas != object_count {esperado}.")
    if file_size not in (None, ""):
        b = os.path.getsize(path)
        if b != int(file_size):
            raise NDError(f"Descarga {path}: {b} bytes != file_size {file_size}.")
    if firma:
        comprobar_firma(path, firma)  # v88
    return {"lineas": lineas, "bytes": os.path.getsize(path)}


def bajar_bulk(meta, out_path, firma):
    for k in ("object_count", "url"):
        if k not in meta:
            raise NDError(f"Falta '{k}' en los datos de la bulk ({sorted(meta)}).")
    tmp = out_path + ".part"
    for intento in range(1, 5):
        try:
            req = urllib.request.Request(meta["url"].strip())
            with urllib.request.urlopen(req, timeout=600, context=_ssl_context()) as r, open(tmp, "wb") as f:
                while True:
                    blk = r.read(1 << 20)
                    if not blk:
                        break
                    f.write(blk)
            break
        except Exception as e:
            if intento == 4:
                raise NDError(f"Descarga de la bulk {meta.get('id')} fallo: {e}.")
            time.sleep(2 ** intento)
    os.replace(tmp, out_path)
    return validar_descarga(out_path, meta["object_count"], meta.get("file_size"), firma)


# ---------------------------------------------------------------------------
# Logica pura
# ---------------------------------------------------------------------------

def en_alcance(p):
    """p = {"tags": [...], "skus": set}. D10, con lineas, sin revision ni despiece."""
    tags_u = [t.strip().upper() for t in p["tags"]]
    if "D10" not in tags_u or not p["skus"]:
        return False
    if any(t in REVISION_UPPER for t in tags_u):
        return False
    if any(t.startswith("DESPIECE") for t in tags_u):
        return False
    return True


def tag_objetivo(p):
    return TAG_MONO if len(p["skus"]) == 1 else TAG_MULTI


def planificar(pedidos):
    r = {"poner": [], "quitar_mal": [], "quitar_fuera": [], "ya_ok": [], "fuera_sin_tag": 0}
    for p in pedidos:
        tiene = [t for t in p["tags"] if t in TAGS_MM]
        if not en_alcance(p):
            if tiene:
                r["quitar_fuera"].append({**_base(p), "quitar": tiene, "motivo": "ya no esta en alcance"})
            else:
                r["fuera_sin_tag"] += 1
            continue
        obj = tag_objetivo(p)
        base = {**_base(p), "tag": obj}
        mal = [t for t in tiene if t != obj]
        if mal:
            r["quitar_mal"].append({**base, "quitar": mal, "motivo": f"lleva {'+'.join(mal)} y corresponde {obj}"})
            if obj not in tiene:
                r["poner"].append({**base, "motivo": "tras quitar el tag equivocado"})
        elif obj in tiene:
            r["ya_ok"].append(base)
        else:
            r["poner"].append(base)
    return r


def _base(p):
    return {"id": p["id"], "name": p["name"], "refs": len(p["skus"]), "skus": sorted(p["skus"])}


def doc_tags(op, ops):
    """op = 'tagsAdd' | 'tagsRemove'; ops = [{"id", "tags":[...]}]"""
    partes = [f"m{i}: {op}(id: {json.dumps(o['id'])}, tags: {json.dumps(o['tags'], ensure_ascii=False)}) "
              "{ userErrors { field message } }" for i, o in enumerate(ops)]
    return "mutation { " + " ".join(partes) + " }"


def leer_pedidos(path):
    ped, orden = {}, []
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        o = json.loads(raw)
        pid = o.get("__parentId")
        if pid is None:
            if "tags" in o:
                ped[o["id"]] = {"id": o["id"], "name": o.get("name"), "tags": list(o.get("tags") or []), "skus": set()}
                orden.append(o["id"])
        elif pid in ped and "unfulfilledQuantity" in o:
            if int(o.get("unfulfilledQuantity") or 0) > 0:
                ped[pid]["skus"].add((o.get("sku") or "").strip() or "?")
    return [ped[i] for i in orden]


# ---------------------------------------------------------------------------
# Comandos
# ---------------------------------------------------------------------------

def _j(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _w(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def hash_canonico():
    with open(os.path.abspath(__file__), "rb") as f:
        t = f.read().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return hashlib.sha256((t + "\n").encode("utf-8")).hexdigest()


def cmd_preparar(a):
    out = a.out
    os.makedirs(out, exist_ok=True)
    t0 = time.time()
    meta = _j(a.bulks)
    pj = os.path.join(out, "mm_pedidos.jsonl")
    val = bajar_bulk(meta["pedidos"], pj, "mm")
    pedidos = leer_pedidos(pj)
    plan = planificar(pedidos)
    rem = {}
    for x in plan["quitar_mal"] + plan["quitar_fuera"]:
        rem[x["id"]] = {"id": x["id"], "tags": x["quitar"], "name": x["name"]}
    add = {x["id"]: {"id": x["id"], "tags": [x["tag"]], "name": x["name"]} for x in plan["poner"]}
    total_ops = len(rem) + len(add)
    tope = a.tope or TOPE_OPS
    lote = a.lote or LOTE_OPS
    ejecutar = total_ops <= tope
    ld = os.path.join(out, "lotes_mm")
    os.makedirs(ld, exist_ok=True)
    for f in os.listdir(ld):
        os.remove(os.path.join(ld, f))
    lotes = []
    if ejecutar:
        for prefijo, op, d in (("mm_r", "tagsRemove", rem), ("mm_a", "tagsAdd", add)):
            lst = list(d.values())
            for i in range(0, len(lst), lote):
                parte = lst[i:i + lote]
                nombre = f"{prefijo}_{i // lote + 1:02d}"
                with open(os.path.join(ld, nombre + ".graphql"), "w", encoding="utf-8") as f:
                    f.write(doc_tags(op, parte))
                _w(os.path.join(ld, nombre + ".json"), parte)
                lotes.append({"lote": nombre, "op": op, "ops": len(parte)})
    _w(os.path.join(out, "plan_mm.json"), {
        "fecha": a.fecha,
        "esperados_quitar": {k: v["tags"] for k, v in rem.items()},
        "esperados_poner": {k: v["tags"][0] for k, v in add.items()}})
    csv_path = os.path.join(out, f"MONO_MULTI_{a.fecha}.csv")
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["pedido", "estado", "tag", "n_referencias", "quitar", "referencias / motivo"])
        for est, lst in (("PONER", plan["poner"]), ("QUITAR_EQUIVOCADO", plan["quitar_mal"]),
                         ("QUITAR_FUERA_DE_ALCANCE", plan["quitar_fuera"]), ("YA_OK", plan["ya_ok"])):
            for p in lst:
                w.writerow([p["name"], est, p.get("tag", ""), p["refs"], " ".join(p.get("quitar", [])),
                            p.get("motivo") or ", ".join(p["skus"])])
    d10 = [p for p in pedidos if "D10" in [t.strip().upper() for t in p["tags"]] and p["skus"]]
    res = {"fecha": a.fecha, "segundos": round(time.time() - t0, 1), "descarga": val,
           "pedidos_abiertos": len(pedidos), "d10_con_lineas": len(d10),
           "en_alcance": sum(1 for p in pedidos if en_alcance(p)),
           "excluidos_revision": sum(1 for p in d10 if any(t.strip().upper() in REVISION_UPPER for t in p["tags"])),
           "excluidos_despiece": sum(1 for p in d10 if any(t.strip().upper().startswith("DESPIECE") for t in p["tags"])),
           "a_poner": len(add), "a_poner_mono": sum(1 for x in plan["poner"] if x["tag"] == TAG_MONO),
           "a_poner_multi": sum(1 for x in plan["poner"] if x["tag"] == TAG_MULTI),
           "quitar_equivocado": len(plan["quitar_mal"]), "quitar_fuera_alcance": len(plan["quitar_fuera"]),
           "ya_ok": len(plan["ya_ok"]), "operaciones": total_ops, "tope": tope, "lote": lote,
           "tope_superado": not ejecutar, "lotes": lotes, "csv": csv_path, "script_sha256": hash_canonico()}
    _w(os.path.join(out, "preparar_mm.json"), res)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if not ejecutar:
        print(f"\nTOPE SUPERADO: {total_ops} operaciones (> {tope}). NO se ha generado ningun lote. Reportar y NO ejecutar.")
    return 0


def cmd_verificar(a):
    plan = _j(os.path.join(a.out, "plan_mm.json"))
    jl = a.jsonl
    if a.bulk:
        jl = os.path.join(a.out, "mm_verificacion.jsonl")
        bajar_bulk(_j(a.bulk), jl, "tags")
    reales = {}
    for raw in open(jl, encoding="utf-8"):
        raw = raw.strip()
        if raw:
            o = json.loads(raw)
            if "__parentId" not in o and "tags" in o:
                reales[o["id"]] = list(o["tags"] or [])
    disc, no_rel, n = {}, [], 0
    for oid, quitados in plan["esperados_quitar"].items():
        n += 1
        if oid not in reales:
            no_rel.append(oid)
        elif any(t in reales[oid] for t in quitados):
            disc[oid] = {"esperado": f"sin {quitados}", "real": reales[oid]}
    if a.hasta == "a":
        for oid, tag in plan["esperados_poner"].items():
            n += 1
            if oid not in reales:
                no_rel.append(oid)
            elif tag not in reales[oid]:
                disc[oid] = {"esperado": tag, "real": reales[oid]}
    print(json.dumps({"hasta": a.hasta, "comprobados": n, "ok": n - len(disc) - len(no_rel),
                      "discrepancias": disc, "no_releidos": no_rel}, ensure_ascii=False, indent=2))
    return 3 if disc else (4 if no_rel else 0)


def _selftest():
    P = lambda name, skus, tags: {"id": "gid://o/" + name, "name": name, "tags": list(tags), "skus": set(skus)}
    assert tag_objetivo(P("a", ["X"], ["D10"])) == TAG_MONO            # 5 unidades de 1 referencia = MONO
    assert tag_objetivo(P("b", ["X", "Y"], ["D10"])) == TAG_MULTI
    assert tag_objetivo(P("c", ["X", "?"], ["D10"])) == TAG_MULTI      # linea sin SKU = otra referencia
    assert en_alcance(P("d", ["X"], ["D10", "<25"]))
    assert not en_alcance(P("e", ["X"], ["MDP"]))                       # sin D10
    assert not en_alcance(P("f", ["X"], ["D10", "REVISANDO"]))
    assert not en_alcance(P("g", ["X"], ["D10", "revision"]))
    assert not en_alcance(P("h", ["X"], ["D10", "REVISIÓN"]))
    assert not en_alcance(P("i", ["X"], ["D10", "DESPIECE"]))
    assert not en_alcance(P("j", ["X"], ["D10", "DESPIECE-REVISAR"]))
    assert not en_alcance(P("k", [], ["D10"]))                          # sin lineas por enviar
    pl = planificar([
        P("n1", ["X"], ["D10"]),                         # poner MONO
        P("n2", ["X", "Y"], ["D10"]),                    # poner MULTI
        P("n3", ["X"], ["D10", "MONO"]),                 # ya ok
        P("n4", ["X", "Y"], ["D10", "MONO"]),            # quitar MONO + poner MULTI
        P("n5", ["X"], ["D10", "MONO", "MULTI"]),        # quitar MULTI (queda MONO)
        P("n6", ["X"], ["MDP", "MONO"]),                 # ya no D10 -> quitar
        P("n7", ["X"], ["D10", "REVISANDO", "MULTI"]),   # revision -> quitar
        P("n8", ["X"], ["MDP"]),                         # fuera sin tag
    ])
    assert [x["name"] for x in pl["poner"]] == ["n1", "n2", "n4"], pl["poner"]
    assert [x["name"] for x in pl["ya_ok"]] == ["n3"]
    assert [x["name"] for x in pl["quitar_mal"]] == ["n4", "n5"]
    assert [x["name"] for x in pl["quitar_fuera"]] == ["n6", "n7"] and pl["fuera_sin_tag"] == 1
    assert pl["quitar_mal"][0]["quitar"] == ["MONO"] and pl["quitar_mal"][1]["quitar"] == ["MULTI"]
    d = doc_tags("tagsAdd", [{"id": "gid://o/1", "tags": ["MONO"]}, {"id": "gid://o/2", "tags": ["MULTI"]}])
    assert d == ('mutation { m0: tagsAdd(id: "gid://o/1", tags: ["MONO"]) { userErrors { field message } } '
                 'm1: tagsAdd(id: "gid://o/2", tags: ["MULTI"]) { userErrors { field message } } }'), d
    assert doc_tags("tagsRemove", [{"id": "gid://o/1", "tags": ["MONO"]}]).startswith("mutation { m0: tagsRemove(")
    _selftest_firma()
    return 6


def _selftest_firma():
    """v88: bulk propia (firma de su query): propias y vacias OK; ajenas -> BulkAjena; exit 5 por el comando real."""
    import pathlib
    import re
    import subprocess
    import tempfile
    for b, q in (("mm", Q_PEDIDOS), ("tags", Q_TAGS)):
        assert all(re.search(r"\b%s\b" % k, q) for k in FIRMAS[b][1]), b
    O = "gid://shopify/Order/1"
    mm = [{"id": O, "name": "#s1", "tags": ["D10"]},
          {"id": "gid://shopify/LineItem/1", "sku": "ND-1", "unfulfilledQuantity": 1, "__parentId": O}]
    tgs = [{"id": O, "tags": ["D10", "MONO"]}]
    fifo = [{"id": O, "name": "#s1", "createdAt": "2026-10-06T04:00:00Z", "displayFinancialStatus": "PAID", "tags": [],
             "estadoEntrega": {"value": "PREPARABLE_HOY"}, "entregaEstimada": None},
            {"id": "gid://shopify/LineItem/1", "sku": "ND-1", "unfulfilledQuantity": 1, "__parentId": O}]
    ped_a2 = [{"id": O, "name": "#s1", "createdAt": "2026-10-06T04:00:00Z", "tags": [], "closed": False,
               "displayFulfillmentStatus": "UNFULFILLED"},
              {"id": "gid://shopify/FulfillmentOrder/9", "status": "OPEN", "__parentId": O}]
    with tempfile.TemporaryDirectory() as tmp:
        def fich(filas):
            p = os.path.join(tmp, "b%d.jsonl" % len(os.listdir(tmp)))
            with open(p, "w", encoding="utf-8") as f:
                f.write("".join(json.dumps(x) + "\n" for x in filas))
            return p
        for b, filas in (("mm", mm), ("tags", tgs), ("mm", []), ("tags", [])):
            p = fich(filas)
            validar_descarga(p, len(filas), os.path.getsize(p), b)
        for b, filas, m in (("mm", tgs, "Q_MM <- tags"), ("mm", fifo, "Q_MM <- pedidos de FIFO"),
                            ("mm", ped_a2, "Q_MM <- pedidos de A.2"), ("tags", fifo, "tags <- pedidos de FIFO"),
                            ("tags", mm, "tags <- Q_MM"), ("tags", tgs + [{"__parentId": O, "x": 1}], "tags con fila hija")):
            p = fich(filas)
            try:
                validar_descarga(p, len(filas), os.path.getsize(p), b)
                raise AssertionError(m + " aceptada")
            except BulkAjena as e:
                assert str(e).startswith("CONTENIDO AJENO (%s): línea " % b), e
        _w(os.path.join(tmp, "plan_mm.json"), {"esperados_quitar": {O: ["MULTI"]}, "esperados_poner": {}})
        for filas, rc, txt in ((fifo, 5, "CONTENIDO AJENO (tags): línea 1"), (tgs, 0, '"ok": 1')):
            p = fich(filas)
            mj = os.path.join(tmp, "meta%d.json" % len(os.listdir(tmp)))
            _w(mj, {"id": "gid://shopify/BulkOperation/1", "object_count": str(len(filas)),
                    "file_size": str(os.path.getsize(p)), "url": pathlib.Path(p).as_uri()})
            r = subprocess.run([sys.executable, os.path.abspath(__file__), "verificar", "--out", tmp, "--hasta", "r",
                                "--bulk", mj], capture_output=True, text=True)
            assert r.returncode == rc and txt in r.stdout, (rc, r.returncode, r.stdout[-300:], r.stderr[-300:])
        p = fich(tgs)
        mj = os.path.join(tmp, "meta_prep.json")
        _w(mj, {"pedidos": {"id": "x", "object_count": "1", "file_size": str(os.path.getsize(p)), "url": pathlib.Path(p).as_uri()}})
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "preparar", "--out", os.path.join(tmp, "o"),
                            "--fecha", "06-10-2026", "--bulks", mj], capture_output=True, text=True)
        assert r.returncode == 5 and "CONTENIDO AJENO (mm): línea 1" in r.stdout, (r.returncode, r.stdout[-300:])


def cmd_selftest(_):
    n = _selftest()
    print(f"selftest OK ({n} bloques de comprobaciones)")
    print("script_sha256", hash_canonico())
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("selftest").set_defaults(fn=cmd_selftest)
    p = sp.add_parser("preparar")
    p.add_argument("--out", default="out")
    p.add_argument("--fecha", required=True)
    p.add_argument("--bulks", required=True, help='JSON {"pedidos": {id, object_count, file_size, url}} (vale el de P)')
    p.add_argument("--tope", type=int, default=None, help="tope de operaciones (por defecto 300); SOLO con autorizacion de Bruno")
    p.add_argument("--lote", type=int, default=None, help="alias por mutacion (por defecto 25)")
    p.set_defaults(fn=cmd_preparar)
    v = sp.add_parser("verificar")
    v.add_argument("--out", default="out")
    v.add_argument("--hasta", choices=["r", "a"], required=True, help="r = tras los lotes de quitar; a = tras los de poner (acumulativo)")
    v.add_argument("--bulk", help="JSON de una bulk con id y tags (Q_TAGS)")
    v.add_argument("--jsonl", help="o un JSONL ya descargado")
    v.set_defaults(fn=cmd_verificar)
    a = ap.parse_args(argv)
    if a.cmd == "verificar" and not (a.bulk or a.jsonl):
        ap.error("verificar necesita --bulk o --jsonl")
    try:
        return a.fn(a)
    except BulkAjena as e:
        print("ERROR:", e)
        return 5
    except NDError as e:
        print("ERROR:", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
