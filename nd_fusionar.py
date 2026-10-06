"""
nd_fusionar.py — CAMBIAR ALMACENES EN PEDIDOS, paso 7B (v93, 06/10/2026, Bruno, caso #s147355).

FUSIONAR fulfillment orders: un pedido que tiene DOS O MÁS fulfillment orders ABIERTAS (status OPEN) en el MISMO
almacén y con el MISMO método de entrega se deja con UNA sola (fulfillmentOrderMerge). Así el almacén ve un único
bloque "No preparado" y prepara el pedido de una vez. No cambia de almacén ninguna línea ni ninguna cantidad.

Sin credenciales. Este script NO habla con Shopify: solo procesa archivos. Claude ejecuta los documentos por Composio
(SHOPIFY_GRAPH_QL_ADMIN_EXECUTE), igual que el resto del protocolo.

Entrada: out/final_raw.jsonl — la bulk final de pedidos que `nd_almacenes_composio.py cerrar` ya descargó y validó
(líneas, bytes y firma). No hace falta otra bulk. out/scope.pkl (de `preparar`) para excluir los grupos que no se tocan.

Alcance (un pedido entra si cumple TODO):
  - en la bulk final, no archivado (closed false), pagado (displayFinancialStatus PAID o PARTIALLY_REFUNDED);
  - sin tag DESISTIMIENTO (exacto) ni FRAUDE / CONTRACARGO (exacto, sin importar mayúsculas);
  - en scope.pkl, con grupo que NO sea 11_NO_PAGADO, DESISTIMIENTO, FRAUDE_CONTRACARGO, ARCHIVADO, NO_PENDING ni
    01_SPLIT_D ni 09_DESPIECE (los despieces los gestiona A.7) (pedidos que no estén en scope.pkl —entraron después de `preparar`— NO se tocan);
  - 2+ FOs con status OPEN, con alguna línea con remainingQuantity > 0, mismo almacén (D10 = DHL) y mismo
    deliveryMethod.methodType. Las FOs ON_HOLD, IN_PROGRESS, SCHEDULED, etc. no se tocan.
Tope: 60 pedidos por corrida. Si se supera, NO se genera ningún lote (se informa y se avisa).

USO
    python3 nd_fusionar.py selftest
    python3 nd_fusionar.py preparar --out DIR [--tope N] [--sin-scope]
        -> DIR/fusionar_plan.json, DIR/lotes_fu/fu_NN.graphql (hasta 15 alias m0..), DIR/fu_lectura.graphql
    python3 nd_fusionar.py verificar --out DIR --lectura DIR/fu_lectura.json [--rechazo LOTE:mN="texto" ...]
    python3 nd_fusionar.py verificar --out DIR --bulk DIR/fu_bulk.json [--rechazo ...]   (bulk Q_PEDIDOS fresca; >30 pedidos)
        -> DIR/fusionar.json

SALIDA: 0 ok · 1 ERROR · 2 uso · 3 DISCREPANCIAS (una fusión no quedó como se esperaba) · 4 NO RELEÍDOS · 6 TOPE.
--sin-scope solo para pruebas en sombra (sin scope.pkl no se puede excluir 01_SPLIT_D).
"""
import hashlib
import json
import os
import pickle
import sys
from collections import defaultdict

D10_ALIASES = {"D10", "DHL"}
PAGADOS = {"PAID", "PARTIALLY_REFUNDED"}
BLOQUEO = {"FRAUDE", "CONTRACARGO"}
GRUPOS_FUERA = {"11_NO_PAGADO", "DESISTIMIENTO", "FRAUDE_CONTRACARGO", "ARCHIVADO", "NO_PENDING", "01_SPLIT_D", "09_DESPIECE"}
TOPE = 60
LOTE = 15
MAX_PEDIDOS_LECTURA = 6
FU_FRAG = ("... on Order { id name fulfillmentOrders(first: 30){ nodes { id status assignedLocation { name } "
           "deliveryMethod { methodType } lineItems(first: 50){ nodes { remainingQuantity lineItem { sku } } } } } }")


class NDError(Exception):
    pass


def _canon(n):
    return "D10" if n in D10_ALIASES else n


def _lit(v):
    return json.dumps(v, ensure_ascii=False)


def _j(p):
    return json.load(open(p, encoding="utf-8"))


def _w(p, o):
    json.dump(o, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)


# ---------------------------------------------------------------------------
# Puro
# ---------------------------------------------------------------------------

def leer_bulk(path):
    """{order_gid: {"name","tags","closed","fin","fos": {fo_id: {"status","loc","met","lines": [(sku, q)]}}}}"""
    ords, fos = {}, {}
    for raw in open(path, encoding="utf-8"):
        raw = raw.strip()
        if not raw:
            continue
        o = json.loads(raw)
        tipo = o["id"].split("/")[3]
        if tipo == "Order":
            ords[o["id"]] = {"name": o.get("name"), "tags": list(o.get("tags") or []), "closed": bool(o.get("closed")),
                             "fin": o.get("displayFinancialStatus"), "fos": {}}
        elif tipo == "FulfillmentOrder":
            fos[o["id"]] = {"order": o["__parentId"], "status": o.get("status"),
                            "loc": _canon((o.get("assignedLocation") or {}).get("name")),
                            "met": (o.get("deliveryMethod") or {}).get("methodType"), "lines": []}
        elif tipo == "FulfillmentOrderLineItem":
            fo = fos.get(o["__parentId"])
            if fo is not None:
                fo["lines"].append(((o.get("lineItem") or {}).get("sku"), int(o.get("remainingQuantity") or 0)))
    for fid, fo in fos.items():
        if fo["order"] in ords:
            ords[fo["order"]]["fos"][fid] = {k: v for k, v in fo.items() if k != "order"}
    return ords


def _bloqueado(o):
    t = {str(x).strip() for x in o["tags"]}
    return "DESISTIMIENTO" in t or any(x.upper() in BLOQUEO for x in t)


def grupos_fusionables(fos):
    """[(loc, met, [fo_ids])] con 2+ FOs OPEN con algo pendiente en el mismo almacén y método."""
    g = defaultdict(list)
    for fid, fo in fos.items():
        if fo["status"] == "OPEN" and any(q > 0 for _, q in fo["lines"]):
            g[(fo["loc"], fo["met"])].append(fid)
    return [(loc, met, sorted(ids)) for (loc, met), ids in sorted(g.items(), key=lambda x: str(x[0])) if len(ids) > 1]


def contenido(fos, loc, met):
    """{sku: qty pendiente} en las FOs OPEN de (loc, met)."""
    c = defaultdict(int)
    for fo in fos.values():
        if fo["status"] == "OPEN" and fo["loc"] == loc and fo["met"] == met:
            for s, q in fo["lines"]:
                if q > 0:
                    c[s or "(sin SKU)"] += q
    return dict(c)


def candidatos(ords, grupos_scope):
    """grupos_scope: {order_gid: grupo} o None (sin scope). Devuelve (plan, excluidos{motivo: n})."""
    plan, excl = [], defaultdict(int)
    for oid, o in ords.items():
        gs = grupos_fusionables(o["fos"])
        if not gs:
            continue
        motivo = None
        if o["closed"]:
            motivo = "archivado"
        elif o["fin"] not in PAGADOS:
            motivo = "no pagado"
        elif _bloqueado(o):
            motivo = "DESISTIMIENTO/FRAUDE/CONTRACARGO"
        elif grupos_scope is not None and oid not in grupos_scope:
            motivo = "no está en scope.pkl (entró después de preparar)"
        elif grupos_scope is not None and grupos_scope[oid] in GRUPOS_FUERA:
            motivo = f"grupo {grupos_scope[oid]}"
        if motivo:
            excl[motivo] += 1
            continue
        plan.append({"oid": oid, "pedido": o["name"],
                     "fusiones": [{"loc": loc, "met": met, "fo_ids": ids, "contenido": contenido(o["fos"], loc, met)}
                                  for loc, met, ids in gs]})
    plan.sort(key=lambda p: p["pedido"] or "")
    return plan, dict(excl)


def campo_merge(fusiones):
    entradas = ", ".join("{mergeIntents: [" + ", ".join(f"{{fulfillmentOrderId: {_lit(i)}}}" for i in f["fo_ids"]) + "]}"
                         for f in fusiones)
    return ("fulfillmentOrderMerge(fulfillmentOrderMergeInputs: [" + entradas + "]) "
            "{ fulfillmentOrderMerges { fulfillmentOrder { id status assignedLocation { name } } } "
            "userErrors { field message code } }")


def docs_lotes(plan):
    out = []
    for n, i in enumerate(range(0, len(plan), LOTE), 1):
        trozo = plan[i:i + LOTE]
        doc = "mutation { " + " ".join(f"m{j}: " + campo_merge(p["fusiones"]) for j, p in enumerate(trozo)) + " }"
        out.append({"nombre": f"fu_{n:02d}", "pedidos": [p["pedido"] for p in trozo],
                    "alias": {f"m{j}": p["oid"] for j, p in enumerate(trozo)}, "query": doc})
    return out


def docs_lectura(oids):
    oids = list(oids)
    return ["query { " + " ".join(f"n{k}: node(id: {_lit(o)}) {{ {FU_FRAG} }}" for k, o in enumerate(oids[i:i + MAX_PEDIDOS_LECTURA]))
            + " }" for i in range(0, len(oids), MAX_PEDIDOS_LECTURA)]


def nodos_lectura(obj):
    out = {}

    def walk(x):
        if isinstance(x, dict):
            if str(x.get("id", "")).startswith("gid://shopify/Order/") and "fulfillmentOrders" in x:
                out[x["id"]] = x
                return
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(obj)
    return out


def fos_de_nodo(n):
    return {fo["id"]: {"status": fo.get("status"), "loc": _canon((fo.get("assignedLocation") or {}).get("name")),
                       "met": (fo.get("deliveryMethod") or {}).get("methodType"),
                       "lines": [((l.get("lineItem") or {}).get("sku"), int(l.get("remainingQuantity") or 0))
                                 for l in fo["lineItems"]["nodes"]]}
            for fo in n["fulfillmentOrders"]["nodes"]}


def comprobar(p, fos):
    """None si cada fusión quedó en UNA FO OPEN con el mismo contenido; si no, el motivo."""
    for f in p["fusiones"]:
        abiertas = [fid for fid, fo in fos.items() if fo["status"] == "OPEN" and fo["loc"] == f["loc"] and fo["met"] == f["met"]
                    and any(q > 0 for _, q in fo["lines"])]
        cont = contenido(fos, f["loc"], f["met"])
        if cont != f["contenido"]:
            return f"{f['loc']}/{f['met']}: contenido cambió {f['contenido']} -> {cont}"
        if len(abiertas) != 1:
            return f"{f['loc']}/{f['met']}: quedan {len(abiertas)} FOs abiertas (se esperaba 1)"
    return None


def bajar_bulk(meta, out_path):
    """Descarga la bulk (meta = {"id","object_count","file_size","url"} de Composio) y valida líneas, bytes y firma
    mínima de Q_PEDIDOS (filas raíz Order con displayFulfillmentStatus; hijas FulfillmentOrder/FulfillmentOrderLineItem)."""
    import ssl
    import time
    import urllib.request
    if int(meta.get("object_count") or 0) == 0 and not meta.get("url"):
        open(out_path, "wb").close()
        return 0
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    ctx = ssl.create_default_context(cafile=cafile) if cafile and os.path.exists(cafile) else ssl.create_default_context()
    for intento in range(1, 5):
        try:
            with urllib.request.urlopen(meta["url"].strip(), timeout=600, context=ctx) as r:
                data = r.read()
            break
        except Exception as e:
            if intento == 4:
                raise NDError(f"Descarga de la bulk {meta.get('id')} falló: {e}")
            time.sleep(2 ** intento)
    open(out_path, "wb").write(data)
    lineas = [l for l in data.splitlines() if l.strip()]
    if len(lineas) != int(meta["object_count"]):
        raise NDError(f"Bulk {meta.get('id')}: {len(lineas)} líneas != object_count {meta['object_count']}")
    if meta.get("file_size") not in (None, "") and len(data) != int(meta["file_size"]):
        raise NDError(f"Bulk {meta.get('id')}: {len(data)} bytes != file_size {meta['file_size']}")
    for n, raw in enumerate(lineas, 1):
        o = json.loads(raw)
        tipo = str(o.get("id", "")).split("/")[3] if str(o.get("id", "")).startswith("gid://shopify/") else ""
        bien = (tipo == "Order" and "__parentId" not in o and "displayFulfillmentStatus" in o) or \
               (tipo in ("FulfillmentOrder", "FulfillmentOrderLineItem") and "__parentId" in o)
        if not bien:
            raise NDError(f"CONTENIDO AJENO (pedidos): línea {n} no es de Q_PEDIDOS. Relanzar ESA bulk UNA vez (maestro §2.3).")
    return len(lineas)


def fos_desde_bulk(path):
    """{order_gid: fos} en el formato de fos_de_nodo, a partir de una bulk Q_PEDIDOS."""
    return {oid: o["fos"] for oid, o in leer_bulk(path).items()}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _args(a, valores, banderas=()):
    out, rech, i = {}, {}, 0
    while i < len(a):
        k = a[i].lstrip("-")
        if k in banderas:
            out[k] = True
            i += 1
        elif k == "rechazo" and i + 1 < len(a):
            clave, _, txt = a[i + 1].partition("=")
            rech[clave.strip()] = txt.strip() or "userErrors sin texto"
            i += 2
        elif k in valores and i + 1 < len(a):
            out[k] = a[i + 1]
            i += 2
        else:
            raise NDError(f"argumento desconocido {a[i]}")
    return out, rech


def cmd_preparar(a):
    o, _ = _args(a, ("out", "tope"), ("sin-scope",))
    d = o["out"]
    bulk = os.path.join(d, "final_raw.jsonl")
    if not os.path.exists(bulk):
        raise NDError(f"No está {bulk}: el paso 7B va DESPUÉS de `cerrar` (que descarga y valida la bulk final).")
    sp = os.path.join(d, "scope.pkl")
    if os.path.exists(sp):
        grupos = {r["order"]["id"]: r["group"] for r in pickle.load(open(sp, "rb"))}
    elif o.get("sin-scope"):
        grupos = None
    else:
        raise NDError(f"No está {sp} (lo deja `preparar`). Sin él no se puede excluir 01_SPLIT_D.")
    tope = int(o.get("tope") or TOPE)
    plan, excl = candidatos(leer_bulk(bulk), grupos)
    os.makedirs(os.path.join(d, "lotes_fu"), exist_ok=True)
    for f in os.listdir(os.path.join(d, "lotes_fu")):
        os.remove(os.path.join(d, "lotes_fu", f))
    res = {"script_sha256": hash_canonico(), "candidatos": len(plan), "fusiones": sum(len(p["fusiones"]) for p in plan),
           "fos_a_juntar": sum(len(f["fo_ids"]) for p in plan for f in p["fusiones"]), "excluidos": excl,
           "por_almacen": dict(sorted(defaultdict(int, {}).items())), "tope": tope, "tope_superado": len(plan) > tope,
           "sin_scope": grupos is None, "lotes": []}
    pa = defaultdict(int)
    for p in plan:
        for f in p["fusiones"]:
            pa[f["loc"]] += 1
    res["por_almacen"] = dict(pa)
    lts = [] if res["tope_superado"] else docs_lotes(plan)
    for l in lts:
        open(os.path.join(d, "lotes_fu", l["nombre"] + ".graphql"), "w", encoding="utf-8").write(l["query"])
        res["lotes"].append({"lote": l["nombre"], "n": len(l["pedidos"]), "alias": l["alias"]})
    docs = docs_lectura([p["oid"] for p in plan]) if lts else []
    if docs:
        open(os.path.join(d, "fu_lectura.graphql"), "w", encoding="utf-8").write("\n".join(docs) + "\n")
    _w(os.path.join(d, "fusionar_plan.json"), {"plan": plan, "resumen": res})
    print(json.dumps({k: v for k, v in res.items() if k != "lotes"} | {"lotes": [(l["lote"], l["n"]) for l in res["lotes"]]},
                     ensure_ascii=False, indent=1))
    for p in plan[:80]:
        print(p["pedido"], "; ".join(f"{f['loc']}/{f['met']}: {len(f['fo_ids'])} FOs {f['contenido']}" for f in p["fusiones"]))
    if res["tope_superado"]:
        print(f"TOPE SUPERADO ({len(plan)} > {tope}): NO se generó ningún lote. Informar y avisar (maestro §2.3 paso 7B).")
        raise SystemExit(6)
    if docs:
        print(f"\nRELECTURA: {len(docs)} documento(s) en {os.path.join(d, 'fu_lectura.graphql')} (uno por línea).")


def cmd_verificar(a):
    o, rech = _args(a, ("out", "lectura", "bulk"))
    d = o["out"]
    pl = _j(os.path.join(d, "fusionar_plan.json"))
    plan, res = pl["plan"], pl["resumen"]
    rechazados = {}
    for l in res["lotes"]:
        for alias, oid in l["alias"].items():
            k = f"{l['lote']}:{alias}"
            if k in rech:
                rechazados[oid] = rech[k]
    desconocidos = [k for k in rech if not any(k == f"{l['lote']}:{al}" for l in res["lotes"] for al in l["alias"])]
    if desconocidos:
        raise NDError(f"--rechazo con clave desconocida {desconocidos} (formato LOTE:mN, p. ej. fu_01:m3)")
    if o.get("bulk"):
        pth = os.path.join(d, "fu_bulk_raw.jsonl")
        bajar_bulk(_j(o["bulk"]), pth)
        fos_post = fos_desde_bulk(pth)
    else:
        fos_post = {oid: fos_de_nodo(n) for oid, n in (nodos_lectura(_j(o["lectura"])) if o.get("lectura") else {}).items()}
    ok, malos, aus, rech_out = [], {}, [], []
    for p in plan:
        if p["oid"] in rechazados:
            rech_out.append({"pedido": p["pedido"], "error": rechazados[p["oid"]]})
            continue
        if p["oid"] not in fos_post:
            aus.append(p["pedido"])
            continue
        m = comprobar(p, fos_post[p["oid"]])
        if m:
            malos[p["pedido"]] = m
        else:
            ok.append(p["pedido"])
    out = {"candidatos": len(plan), "fusionados_ok": ok, "rechazos": rech_out, "discrepancias": malos, "no_releidos": aus,
           "excluidos": res["excluidos"], "por_almacen": res["por_almacen"]}
    _w(os.path.join(d, "fusionar.json"), out)
    print(f"FUSIONAR: {len(ok)} ok, {len(rech_out)} rechazos, {len(malos)} discrepancias, {len(aus)} no releídos "
          f"(de {len(plan)} candidatos)")
    for k, v in malos.items():
        print("  DISCREPANCIA", k, v)
    for r in rech_out:
        print("  RECHAZO", r["pedido"], r["error"])
    if malos:
        raise SystemExit(3)
    if aus:
        print("NO RELEÍDOS:", ", ".join(aus[:40]))
        raise SystemExit(4)


# ---------------------------------------------------------------------------
# Selftest + hash
# ---------------------------------------------------------------------------

def hash_canonico():
    with open(os.path.abspath(__file__), "rb") as f:
        t = f.read().decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").rstrip()
    return hashlib.sha256((t + "\n").encode("utf-8")).hexdigest()


def _selftest():
    import contextlib
    import io
    import tempfile
    fallos = []

    def ok(c, m):
        if not c:
            fallos.append(m)
    n = [0]

    def ped(name, fos, tags=(), fin="PAID", closed=False):
        """fos: [(status, loc, met, [(sku, q)])]"""
        n[0] += 1
        og = f"gid://shopify/Order/{n[0]}"
        filas = [{"id": og, "name": name, "createdAt": "2026-10-06T00:00:00Z", "tags": list(tags), "closed": closed,
                  "displayFinancialStatus": fin, "displayFulfillmentStatus": "UNFULFILLED"}]
        for st, loc, met, ls in fos:
            n[0] += 1
            fg = f"gid://shopify/FulfillmentOrder/{n[0]}"
            filas.append({"id": fg, "status": st, "assignedLocation": {"name": loc}, "deliveryMethod": {"methodType": met},
                          "__parentId": og})
            for sku, q in ls:
                n[0] += 1
                filas.append({"id": f"gid://shopify/FulfillmentOrderLineItem/{n[0]}", "remainingQuantity": q,
                              "lineItem": {"sku": sku, "name": "x"}, "__parentId": fg})
        return og, filas
    S = "SHIPPING"
    casos = {
        "#s147355": ped("#s147355", [("OPEN", "D10", S, [("ND-0731-WALNUT", 1)]), ("OPEN", "D10", S, [("ND-0923-GREEN", 4)])]),
        "#dhl_d10": ped("#dhl_d10", [("OPEN", "DHL", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])]),
        "#tres": ped("#tres", [("OPEN", "Terrassa", S, [("A", 1)]), ("OPEN", "Terrassa", S, [("B", 1)]),
                               ("OPEN", "Terrassa", S, [("C", 2)]), ("OPEN", "D10", S, [("D", 1)])]),
        "#dos_alm": ped("#dos_alm", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "Matadepera", S, [("B", 1)])]),
        "#metodo": ped("#metodo", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", "PICK_UP", [("B", 1)])]),
        "#hold": ped("#hold", [("OPEN", "D10", S, [("A", 1)]), ("ON_HOLD", "D10", S, [("B", 1)])]),
        "#vacia": ped("#vacia", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 0)])]),
        "#cerrada": ped("#cerrada", [("OPEN", "D10", S, [("A", 1)]), ("CLOSED", "D10", S, [("B", 1)])]),
        "#nopagado": ped("#nopagado", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])], fin="PENDING"),
        "#reemb": ped("#reemb", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])], fin="PARTIALLY_REFUNDED"),
        "#fraude": ped("#fraude", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])], tags=["fraude"]),
        "#desist": ped("#desist", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])], tags=["DESISTIMIENTO"]),
        "#fraudealto": ped("#fraudealto", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])], tags=["FRAUDE ALTO"]),
        "#archivado": ped("#archivado", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])], closed=True),
        "#rotura": ped("#rotura", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])]),
        "#despiece": ped("#despiece", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("PZ-1", 1)])]),
        "#nuevo": ped("#nuevo", [("OPEN", "D10", S, [("A", 1)]), ("OPEN", "D10", S, [("B", 1)])]),
    }
    oid = {k: v[0] for k, v in casos.items()}
    grupos = {o: "02_D10_ENTERO_SALE_YA" for k, o in oid.items() if k != "#nuevo"}
    grupos[oid["#rotura"]] = "01_SPLIT_D"
    grupos[oid["#despiece"]] = "09_DESPIECE"
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "final_raw.jsonl")
        open(p, "w", encoding="utf-8").write("".join(json.dumps(x) + "\n" for _, f in casos.values() for x in f))
        ords = leer_bulk(p)
        plan, excl = candidatos(ords, grupos)
        nombres = sorted(x["pedido"] for x in plan)
        ok(nombres == ["#dhl_d10", "#fraudealto", "#reemb", "#s147355", "#tres"], f"candidatos {nombres}")
        ok(excl == {"no pagado": 1, "DESISTIMIENTO/FRAUDE/CONTRACARGO": 2, "archivado": 1, "grupo 01_SPLIT_D": 1, "grupo 09_DESPIECE": 1,
                    "no está en scope.pkl (entró después de preparar)": 1}, f"excluidos {excl}")
        pt = {x["pedido"]: x for x in plan}
        ok(len(pt["#tres"]["fusiones"]) == 1 and len(pt["#tres"]["fusiones"][0]["fo_ids"]) == 3
           and pt["#tres"]["fusiones"][0]["contenido"] == {"A": 1, "B": 1, "C": 2}, f"#tres {pt['#tres']}")
        ok(pt["#s147355"]["fusiones"][0]["contenido"] == {"ND-0731-WALNUT": 1, "ND-0923-GREEN": 4}, "contenido #s147355")
        ok(len(candidatos(ords, None)[0]) == 8, "sin scope: SPLIT_D, despiece y nuevo entran (solo sombra)")
        # documentos
        lts = docs_lotes(plan)
        q = lts[0]["query"]
        ok(len(lts) == 1 and q.startswith("mutation { m0: fulfillmentOrderMerge(") and q.count("fulfillmentOrderMerge(") == 5
           and q.count("mergeIntents") == 5 and "userErrors { field message code }" in q, f"doc {q[:120]}")
        ok(len(docs_lotes(plan * 4)) == 2, "lotes de 15")
        dl = docs_lectura([f"gid://shopify/Order/{k}" for k in range(13)])
        ok(len(dl) == 3 and all(x.count("node(id:") <= MAX_PEDIDOS_LECTURA for x in dl), "lectura partida en <= 6")
        # CLI extremo a extremo
        pickle.dump([{"order": {"id": o}, "group": g} for o, g in grupos.items()], open(os.path.join(tmp, "scope.pkl"), "wb"))

        def run(fn, *args):
            buf, code = io.StringIO(), 0
            with contextlib.redirect_stdout(buf):
                try:
                    fn(["--out", tmp, *args])
                except SystemExit as e:
                    code = e.code
            return buf.getvalue(), code
        out, c = run(cmd_preparar)
        ok(c == 0 and os.path.exists(os.path.join(tmp, "lotes_fu", "fu_01.graphql")) and os.path.exists(os.path.join(tmp, "fu_lectura.graphql")),
           f"preparar {c} {out[-200:]}")
        out, c = run(cmd_preparar, "--tope", "3")
        ok(c == 6 and not os.listdir(os.path.join(tmp, "lotes_fu")), f"tope: sin lotes y exit 6 ({c})")
        run(cmd_preparar)

        def nodo(o, fos):
            return {"id": o, "name": o, "fulfillmentOrders": {"nodes": [
                {"id": f, "status": st, "assignedLocation": {"name": loc}, "deliveryMethod": {"methodType": met},
                 "lineItems": {"nodes": [{"remainingQuantity": q, "lineItem": {"sku": s}} for s, q in ls]}} for f, st, loc, met, ls in fos]}}
        bien = {
            oid["#s147355"]: nodo(oid["#s147355"], [("F1", "OPEN", "D10", S, [("ND-0731-WALNUT", 1), ("ND-0923-GREEN", 4)]),
                                                    ("F2", "CLOSED", "D10", S, [("ND-0923-GREEN", 0)])]),
            oid["#dhl_d10"]: nodo(oid["#dhl_d10"], [("F3", "OPEN", "D10", S, [("A", 1), ("B", 1)])]),
            oid["#tres"]: nodo(oid["#tres"], [("F4", "OPEN", "Terrassa", S, [("A", 1), ("B", 1), ("C", 2)]), ("F5", "OPEN", "D10", S, [("D", 1)])]),
            oid["#reemb"]: nodo(oid["#reemb"], [("F6", "OPEN", "D10", S, [("A", 1), ("B", 1)])]),
        }
        _w(os.path.join(tmp, "l.json"), [{"n0": v} for v in bien.values()])
        out, c = run(cmd_verificar, "--lectura", os.path.join(tmp, "l.json"))
        ok(c == 4 and "4 ok" in out and "#fraudealto" in out, f"falta releer #fraudealto -> exit 4: {c} {out[:120]}")
        al = next(a for a, o in _j(os.path.join(tmp, "fusionar_plan.json"))["resumen"]["lotes"][0]["alias"].items() if o == oid["#fraudealto"])
        out, c = run(cmd_verificar, "--lectura", os.path.join(tmp, "l.json"), "--rechazo", f"fu_01:{al}=no se puede fusionar")
        ok(c == 0 and "4 ok, 1 rechazos, 0 discrepancias" in out, f"rechazo por alias: {c} {out[:120]}")
        mal = dict(bien)
        mal[oid["#dhl_d10"]] = nodo(oid["#dhl_d10"], [("F3", "OPEN", "D10", S, [("A", 1)]), ("F7", "OPEN", "D10", S, [("B", 1)])])
        mal[oid["#tres"]] = nodo(oid["#tres"], [("F4", "OPEN", "Terrassa", S, [("A", 1), ("B", 1)]), ("F5", "OPEN", "D10", S, [("D", 1)])])
        _w(os.path.join(tmp, "l.json"), [{"n0": v} for v in mal.values()])
        out, c = run(cmd_verificar, "--lectura", os.path.join(tmp, "l.json"), "--rechazo", f"fu_01:{al}=x")
        ok(c == 3 and "2 discrepancias" in out and "quedan 2 FOs" in out and "contenido cambió" in out, f"discrepancias: {c} {out[:200]}")
        # verificar por bulk (URL file://): #s147355 fusionado + los demás sin fusionar -> discrepancias
        import pathlib
        post = [x for x in casos["#s147355"][1] if x["id"].split("/")[3] == "Order"]
        post += [{"id": "gid://shopify/FulfillmentOrder/990", "status": "OPEN", "assignedLocation": {"name": "D10"},
                  "deliveryMethod": {"methodType": S}, "__parentId": oid["#s147355"]},
                 {"id": "gid://shopify/FulfillmentOrderLineItem/991", "remainingQuantity": 1, "lineItem": {"sku": "ND-0731-WALNUT"}, "__parentId": "gid://shopify/FulfillmentOrder/990"},
                 {"id": "gid://shopify/FulfillmentOrderLineItem/992", "remainingQuantity": 4, "lineItem": {"sku": "ND-0923-GREEN"}, "__parentId": "gid://shopify/FulfillmentOrder/990"}]
        for k in ("#dhl_d10", "#tres", "#reemb", "#fraudealto"):
            post += casos[k][1]
        bp = os.path.join(tmp, "post.jsonl")
        open(bp, "w", encoding="utf-8").write("".join(json.dumps(x) + "\n" for x in post))
        _w(os.path.join(tmp, "b.json"), {"id": "B", "object_count": str(len(post)), "file_size": str(os.path.getsize(bp)), "url": pathlib.Path(bp).as_uri()})
        out, c = run(cmd_verificar, "--bulk", os.path.join(tmp, "b.json"))
        ok(c == 3 and "1 ok" in out and "4 discrepancias" in out, f"verificar --bulk: {c} {out[:160]}")
        ajena = [{"id": oid["#s147355"], "tags": []}]
        open(bp, "w", encoding="utf-8").write("".join(json.dumps(x) + "\n" for x in ajena))
        _w(os.path.join(tmp, "b.json"), {"id": "B", "object_count": "1", "file_size": str(os.path.getsize(bp)), "url": pathlib.Path(bp).as_uri()})
        try:
            run(cmd_verificar, "--bulk", os.path.join(tmp, "b.json"))
            ok(False, "bulk ajena aceptada")
        except NDError as e:
            ok("CONTENIDO AJENO" in str(e), f"bulk ajena: {e}")
        try:
            run(cmd_verificar, "--lectura", os.path.join(tmp, "l.json"), "--rechazo", "fu_09:m0=x")
            ok(False, "rechazo con clave desconocida aceptado")
        except NDError:
            pass
    return fallos


def cmd_selftest(_):
    f = _selftest()
    if f:
        print("SELFTEST FALLIDO — NO seguir con el paso 7B.")
        for x in f:
            print("   ", x)
        raise SystemExit(1)
    print("SELFTEST OK")
    print(f"SHA256 canonico nd_fusionar.py: {hash_canonico()}")


COMANDOS = {"selftest": cmd_selftest, "preparar": cmd_preparar, "verificar": cmd_verificar}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMANDOS:
        print("Uso: python3 nd_fusionar.py {" + "|".join(COMANDOS) + "} [args]")
        raise SystemExit(2)
    try:
        COMANDOS[sys.argv[1]](sys.argv[2:])
    except NDError as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)
