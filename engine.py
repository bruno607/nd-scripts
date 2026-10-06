import json, re, sys, unicodedata
from collections import defaultdict
from datetime import datetime

DESPIECE_TOKENS = ["PIEZA","BANDEJA","TABLERO","TORNILL","PATA","REPOSABRAZOS","REPOSACABEZA","KIT-","GASLIFT","BASE-ASIENTO"]
# Nota (26/08 noche, quinta revision): "-PATA" paso a "PATA" (sin exigir el guion) porque
# Northdeco confirmo que "PROTECTOR DE PATA - ND-0776-WALNUT -" (sku vacio, #s137042) es un
# repuesto real y el patron con guion no lo detectaba. Verificado contra todo el universo de
# lineas activas: los unicos otros titulos/skus que contienen "PATA" ya empezaban con el
# prefijo "PZ-" (repuestos con SKU propio), asi que ampliar el token no genero ningun falso
# positivo nuevo — y como el titulo solo se consulta cuando el SKU viene vacio (ver
# is_despiece), el riesgo de capturar un producto entero normal (con SKU real) sigue acotado.

# ALIAS DE ALMACEN "DHL" -> "D10" (v51, 08/09/2026, aviso de Northdeco): el almacen que
# Shopify muestra hoy como Location "DHL" se va a renombrar IN-PLACE (mismo id de Shopify,
# solo cambia el "name") a "D10" hoy mismo a la tarde/noche, y Northdeco va a re-tagear en
# bloque todos los pedidos abiertos que tengan el tag "DHL" a "D10" la misma noche. Como la
# corrida de las 13:00 del 08/09 corre ANTES del cambio y la de las 4:00 AM del 09/09 corre
# DESPUES, este motor trata "DHL" y "D10" como el mismo almacen fisico en TODO lo que compara
# contra datos reales de Shopify (nombre de Location de un FulfillmentOrder, nombre de
# ubicacion en el bulk de inventario): cualquier "DHL" que todavia devuelva la API se
# normaliza a "D10" ni bien se lee (`normalize_loc_name`), asi el resto del motor solo conoce
# el nombre canonico nuevo y no le importa si Shopify ya completo el rename o no en el momento
# exacto de la corrida. El GID de la Location NO cambia (rename in-place, confirmado por
# Northdeco) — por eso `LOC_IDS["D10"]` sigue apuntando al mismo `gid` de siempre.
D10_ALIASES = {"DHL", "D10"}
CANONICAL_D10 = "D10"


def normalize_loc_name(name):
    """Traduce cualquier nombre de Location leido de Shopify a su forma canonica interna.
    Hoy (antes del rename) Shopify puede devolver 'DHL'; despues del rename va a devolver
    'D10' — en ambos casos el motor trabaja siempre con 'D10' de aca en mas."""
    return CANONICAL_D10 if name in D10_ALIASES else name


LOC_IDS = {
    "Almacén Principal": "gid://shopify/Location/58556219571",
    "D10": "gid://shopify/Location/111265644930",  # ex "DHL" — Location renombrada 08/09/2026, mismo GID
    "Matadepera": "gid://shopify/Location/111219868034",
    "Terrassa": "gid://shopify/Location/104513241474",
}
BUFFERS = {"Terrassa": 3, "Matadepera": 3, "Almacén Principal": 6, "D10": 0}
# ORDEN DE ALMACENES (v72, 27/09/2026, decision de Bruno en vivo): Terrassa pasa a ser el
# ULTIMO recurso entre los propios, no el primero. Northdeco quiere que salgan desde Terrassa
# la menor cantidad posible de pedidos: todo lo que Terrassa no tenga se manda a D10 en vez de
# a Matadepera/Almacen Principal primero (ver la prioridad NUEVA mas abajo, D10 -> Matadepera ->
# Almacen Principal -> Terrassa). Terrassa sigue siendo el unico destino cuando es el UNICO
# almacen (propio o D10) con stock real para una linea. LOCAL_ORDER en si solo fija el orden
# ENTRE los tres almacenes propios (para build_raw_available/compute_usable_pool, donde el
# orden no importa, y para los loops de reparto de mas abajo, donde ahora Terrassa va al final).
LOCAL_ORDER = ["Matadepera", "Almacén Principal", "Terrassa"]

def is_despiece(sku, title=None):
    # CRITERIO UNICO DE DESPIECE (v90, 06/10/2026, decision de Bruno; misma regla y mismos
    # casos de prueba en A.2, A.3 y A.7 — ver CRITERIO_DESPIECE / maestro §1):
    #   - Con SKU (tras strip) manda el SKU: empieza por "PZ-" o contiene un token. El
    #     titulo NO se mira ("Silla ... con Reposabrazos" con SKU real es una silla entera).
    #   - Sin SKU: el titulo empieza por "PZ-", contiene "DESPIECE" o contiene un token.
    # Las lineas placeholder ("ENVIO DIVIDIDO #s...") se excluyen ANTES (is_placeholder_line).
    s = (sku or "").strip().upper()
    if s:
        return s.startswith("PZ-") or any(tok in s for tok in DESPIECE_TOKENS)
    t = (title or "").strip().upper()
    return t.startswith("PZ-") or "DESPIECE" in t or any(tok in t for tok in DESPIECE_TOKENS)

def is_placeholder_line(sku, name):
    # Lineas que no son producto real: los "ENVIO DIVIDIDO #sXXXXX" que Shopify crea
    # como linea de pedido para representar un envio partido en varias direcciones.
    # No tienen SKU y no deben contarse ni como normal ni como despiece: no se tocan.
    if sku:
        return False
    if not name:
        return False
    return name.strip().upper().startswith("ENVIO DIVIDIDO")

def parse_orders(path):
    orders = {}
    fos = {}
    lines = {}
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            gid = obj["id"]
            typ = gid.split("/")[3]
            parent = obj.get("__parentId")
            if typ == "Order":
                # customer/sourceName/totalPriceSet: agregados 29/08 para el reporte de ATC
                # (pedidos Pendiente/Parcialmente pagado) — ver build_changes_preview.py.
                # Son campos escalares/objeto simple del Order, vienen embebidos en la misma
                # linea del bulk export (no generan lineas hijas separadas).
                customer = obj.get("customer") or {}
                total_set = (obj.get("totalPriceSet") or {}).get("shopMoney") or {}
                orders[gid] = {"id": gid, "name": obj["name"], "createdAt": obj["createdAt"],
                                "tags": obj.get("tags", []), "closed": obj.get("closed", False),
                                "financial_status": obj.get("displayFinancialStatus"),
                                "customer_name": customer.get("displayName"),
                                "channel": obj.get("sourceName"),
                                "total_amount": total_set.get("amount"),
                                "total_currency": total_set.get("currencyCode"),
                                # displayFulfillmentStatus (agregado 23/09/2026, v68): lo usa
                                # diff.py para el tag ENVIO PARCIAL. Escalar inline del Order,
                                # no suma objetos a la bulk. Si la query no lo trae (query
                                # vieja), queda en None y diff.py NO toca ENVIO PARCIAL.
                                "fulfillment_status": obj.get("displayFulfillmentStatus"),
                                # v89 (06/10/2026, prioridad Repo): titulo de la linea de envio y
                                # codigos de descuento. Inline en el Order (no suman objetos a la bulk).
                                # shippingLine puede venir null (pedido sin envio) -> None.
                                "shipping_title": (obj.get("shippingLine") or {}).get("title"),
                                "discount_codes": list(obj.get("discountCodes") or []),
                                "fos": []}
            elif typ == "FulfillmentOrder":
                fos[gid] = {"id": gid, "status": obj["status"],
                             "location": normalize_loc_name((obj.get("assignedLocation") or {}).get("name")),
                             "deliveryMethod": (obj.get("deliveryMethod") or {}).get("methodType"),
                             "order_id": parent, "lines": []}
            elif typ == "FulfillmentOrderLineItem":
                li = obj.get("lineItem") or {}
                lines[obj["id"]] = {"id": obj["id"], "remainingQuantity": obj.get("remainingQuantity", 0),
                                     "sku": li.get("sku"), "name": li.get("name"), "fo_id": parent}
    for lid, l in lines.items():
        fo = fos.get(l["fo_id"])
        if fo:
            fo["lines"].append(l)
    for fid, fo in fos.items():
        o = orders.get(fo["order_id"])
        if o:
            o["fos"].append(fo)
    return orders

def parse_inventory(path):
    # sku -> location -> {available, on_hand, incoming, committed}
    stock = defaultdict(lambda: defaultdict(lambda: {"available":0,"on_hand":0,"incoming":0,"committed":0}))
    cur_sku = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            obj = json.loads(raw)
            if "sku" in obj and "id" in obj and obj["id"].split("/")[3] == "ProductVariant":
                cur_variant_id = obj["id"]
                cur_sku = obj.get("sku")
                variant_sku_map[cur_variant_id] = cur_sku
            elif "location" in obj:
                parent = obj["__parentId"]
                sku = variant_sku_map.get(parent)
                if not sku:
                    continue
                loc = normalize_loc_name(obj["location"]["name"])
                q = {qq["name"]: qq["quantity"] for qq in obj["quantities"]}
                stock[sku][loc]["available"] += q.get("available", 0)
                stock[sku][loc]["on_hand"] += q.get("on_hand", 0)
                stock[sku][loc]["incoming"] += q.get("incoming", 0)
                stock[sku][loc]["committed"] += q.get("committed", 0)
    return stock

variant_sku_map = {}

def build_raw_available(stock):
    # available crudo (tal cual lo reporta Shopify, sin colchon ni piso en 0) por
    # sku/almacen, incluido D10 (ex DHL, ver alias mas arriba). Es la base para todo lo
    # demas: el colchon/piso se aplica UNA SOLA VEZ, en compute_usable_pool, despues de que
    # free_unpaid_reservations haya ajustado estos numeros crudos.
    raw_available = defaultdict(dict)
    incoming_dhl = defaultdict(int)
    for sku, locs in stock.items():
        for loc in LOCAL_ORDER:
            raw_available[sku][loc] = locs.get(loc, {}).get("available", 0)
        raw_available[sku]["D10"] = locs.get("D10", {}).get("available", 0)
        # incoming: sum incoming across ALL locations, treated as D10 incoming
        total_incoming = sum(l.get("incoming", 0) for l in locs.values())
        incoming_dhl[sku] = total_incoming
    return raw_available, incoming_dhl

def build_dhl_on_hand(stock):
    # on_hand CRUDO en D10 (ex DHL) por sku (sin restar committed, sin colchon). Se usa SOLO
    # para la excepcion "el pedido ya esta entero en D10" (ver classify_order) —
    # "available" (on_hand - committed) NO sirve ahi porque para un pedido cuyo FO ya
    # esta en D10, su propia reserva YA forma parte de "committed": comparar contra
    # "available" es preguntarle a un cajero comprometido si hay caja libre ADEMAS de
    # lo que el mismo ya tiene reservado, y esa pregunta siempre da 0/negativo aunque
    # el stock fisico este ahi mismo, ya asignado a este pedido. Lo que hay que saber
    # es si el on_hand fisico alcanza para cubrir, en orden de antiguedad, a todos los
    # pedidos que YA estan comprometidos en D10 para ese SKU — por eso se consume como
    # pool FIFO (igual que `pool`/`dhl_available`/`incoming_dhl`) a medida que se
    # procesan los pedidos en orden de creacion, no se resta "committed" de entrada.
    dhl_on_hand = defaultdict(int)
    for sku, locs in stock.items():
        dhl_on_hand[sku] = locs.get("D10", {}).get("on_hand", 0)
    return dhl_on_hand

def compute_usable_pool(raw_available):
    # usable pool per sku per local warehouse = max(available_crudo - buffer, 0).
    # Se llama DESPUES de free_unpaid_reservations, para que el colchon/piso en 0
    # se aplique sobre el numero ya corregido (crudo + liberado), no antes — aplicar
    # el piso antes y despues "sumar" la reserva liberada podia inventar stock
    # fantasma en un almacen sobrevendido (ver §9.25: bug de #s135095/AP).
    pool = defaultdict(dict)
    dhl_available = defaultdict(int)
    for sku, locs in raw_available.items():
        for loc in LOCAL_ORDER:
            pool[sku][loc] = max(locs.get(loc, 0) - BUFFERS[loc], 0)
        dhl_available[sku] = max(locs.get("D10", 0) - BUFFERS["D10"], 0)
    return pool, dhl_available

PAID_STATUSES = {"PAID", "PARTIALLY_REFUNDED"}
# v90: tags de bloqueo de A.3 que dejan el pedido fuera de scope (ver classify_order).
BLOQUEO_FUERA_DE_SCOPE = {"FRAUDE", "CONTRACARGO"}
# Grupos fuera de scope: no se tocan ni se cuentan (engine.py y diff.py).
FUERA_DE_SCOPE = ("DESISTIMIENTO", "FRAUDE_CONTRACARGO", "NO_PENDING", "ARCHIVADO", "11_NO_PAGADO")
# Pedidos con este tag son prioritarios (decision de Northdeco, 26/08 noche): cuando
# el stock no alcanza para todos ("descarga"/reparto ajustado), estos pedidos deben
# quedarse con el stock disponible ANTES que cualquier otro pedido, sin importar la
# fecha de creacion de cada uno. Todo el reparto de pools (locales, D10, incoming) se
# hace en un solo pase, en el orden en que se procesan los pedidos — por eso la forma
# de darles prioridad es procesarlos primero, antes que el resto, en vez de competir
# por orden de creacion como los demas (ver el sort en main()).
PRIORITY_TAGS = {"b2bpartnership"}  # comparacion normalizada (ver _norm_tag)

def _norm_tag(t):
    # minusculas y sin espacios/guiones/signos: "B2B Partnership", "b2b-partnership", "B2B_Partnership" -> "b2bpartnership"
    return "".join(ch for ch in str(t).lower() if ch.isalnum())

# PRIORIDAD REPO (v89, 06/10/2026, decision de Bruno): los pedidos de REPOSICION ("repo",
# p. ej. #s147320, draft de 0 EUR con envio "Repo rotura s141353") van POR DELANTE de B2B
# Partnership: orden Repo > B2B Partnership > resto (dentro de cada grupo, por createdAt).
# Un pedido es repo si el titulo de su linea de envio O alguno de sus codigos de descuento
# empieza por "repo" como palabra: tras quitar espacios iniciales, mayusculas y tildes,
# "reposicion..." o "repo" seguido de fin / no-letra ("repo", "repo rotura", "repo-x",
# "repo_x", "repo2"). NO cuentan "report", "reportaje", "reposo", "repositorio".
_REPO_RE = re.compile(r"repo(?:sicion|$|[^a-z])")

def _sin_tildes(t):
    return "".join(c for c in unicodedata.normalize("NFKD", str(t)) if not unicodedata.combining(c))

def es_texto_repo(t):
    return bool(t) and bool(_REPO_RE.match(_sin_tildes(t).strip().lower()))

def es_repo(order):
    return es_texto_repo(order.get("shipping_title")) or any(es_texto_repo(c) for c in order.get("discount_codes") or [])

def es_b2b(order):
    return bool(PRIORITY_TAGS & {_norm_tag(t) for t in order.get("tags", [])})

def sort_key(o):
    # 0 = repo, 1 = B2B Partnership, 2 = resto; dentro de cada grupo, por createdAt.
    # Sin pedidos repo el orden es identico al de v84 (B2B antes que el resto).
    return (0 if es_repo(o) else 1 if es_b2b(o) else 2, o["createdAt"])

def classify_order(order, pool, dhl_available, incoming_dhl, raw_available, dhl_on_hand):
    tags = set(order["tags"])
    # Pedido no pagado (pending, partially_paid, authorized, refunded, voided, expired...):
    # no se toca ni se cuenta hasta que este PAID o PARTIALLY_REFUNDED. Antes esto se
    # filtraba en silencio en la propia query de extraccion (status:open AND financial_status:
    # paid OR partially_refunded) — se deja como grupo explicito para que quede trazado.
    fstatus = order.get("financial_status")
    if fstatus is not None and fstatus not in PAID_STATUSES:
        return {"group": "11_NO_PAGADO", "skip": True}
    if "DESISTIMIENTO" in tags:
        return {"group": "DESISTIMIENTO", "skip": True}
    # v90 (06/10/2026, decision de Bruno): FRAUDE y CONTRACARGO fuera de scope, igual que
    # DESISTIMIENTO (no se tocan tags, almacen ni FOs). A.3 los bloquea quitando RESERVA y
    # los SKU; antes A.2 se los volvia a poner (vaiven). Se reconocen como los reconoce A.3
    # (BLOQUEO_TAGS: tag exacto, sin importar mayusculas). No se amplia a otras grafias.
    if any(str(t).upper() in BLOQUEO_FUERA_DE_SCOPE for t in tags):
        return {"group": "FRAUDE_CONTRACARGO", "skip": True}
    # Pedido archivado (Shopify "closed": true) — Northdeco ya lo dio por resuelto
    # a mano (preparado o gestionado por otra via), aunque el FO siga mostrando
    # remainingQuantity > 0. No tocar tags ni almacen, no contarlo en el scope.
    if order.get("closed"):
        return {"group": "ARCHIVADO", "skip": True}

    active_fos = []
    for fo in order["fos"]:
        if fo["status"] in ("CLOSED", "CANCELLED"):
            continue
        pend_lines = [l for l in fo["lines"] if l["remainingQuantity"] and l["remainingQuantity"] > 0]
        if pend_lines:
            active_fos.append((fo, pend_lines))

    if not active_fos:
        return {"group": "NO_PENDING", "skip": True}

    # pick up
    if any(fo["deliveryMethod"] == "PICK_UP" for fo, _ in active_fos):
        return {"group": "10_TIENDA", "skip": False, "tag_target": "TIENDA"}

    # split lines into despiece vs normal, descartando lineas placeholder (sin SKU,
    # tipo "ENVIO DIVIDIDO #sXXXXX") que no son producto real
    normal_lines = []
    despiece_lines = []
    for fo, pls in active_fos:
        for l in pls:
            if is_placeholder_line(l["sku"], l["name"]):
                continue
            entry = {"sku": l["sku"], "name": l["name"], "qty": l["remainingQuantity"],
                      "fo_id": fo["id"], "fo_location": fo["location"], "line_id": l["id"]}
            if is_despiece(l["sku"], l["name"]):
                despiece_lines.append(entry)
            else:
                normal_lines.append(entry)

    if not normal_lines and not despiece_lines:
        # todas las lineas pendientes eran placeholders (p.ej. "ENVIO DIVIDIDO") —
        # no hay nada real que decidir para este pedido.
        return {"group": "NO_PENDING", "skip": True}

    if not normal_lines:
        return {"group": "09_DESPIECE", "skip": False, "despiece_lines": despiece_lines}

    # exception: whole order already entirely at D10 (all normal lines currently at D10)
    already_all_dhl = all(l["fo_location"] == "D10" for l in normal_lines)

    decisions = []
    for l in normal_lines:
        sku = l["sku"]; qty = l["qty"]
        if already_all_dhl:
            # Si D10 SI puede completar la linea, se queda ahi (no tiene sentido moverla
            # fisicamente si ya esta donde puede salir). Corregido (26/08 noche, octava
            # revision, tras pregunta de Northdeco sobre #s128615/#s131831/#s133349/
            # #s135744): se compara contra `dhl_on_hand` (fisico crudo, consumido FIFO
            # por antiguedad), NO contra `dhl_available` (on_hand - committed). Para un
            # pedido cuyo FO YA esta en D10, su propia reserva ya forma parte de
            # "committed" — comparar contra "available" pregunta si sobra stock ADEMAS
            # de lo que el mismo pedido ya tiene asignado, lo cual da 0 o negativo
            # incluso cuando el stock fisico esta ahi mismo, cubriendolo perfectamente.
            if dhl_on_hand.get(sku, 0) >= qty:
                dhl_on_hand[sku] -= qty
                decisions.append({**l, "motivo": "stock D10", "destino": "D10"})
                continue
            # D10 NO puede completarla (sin stock real ahi, aunque este "comprometida"/
            # sobrevendida). Antes esto se dejaba fijo en D10 solo por estar ya ahi; eso
            # dejaba pedidos varados en D10 en negativo mientras habia stock real y libre
            # en un almacen propio. Corregido: si algun almacen local SI puede completarla,
            # se reasigna ahi (se saca de D10).
            placed_local = False
            for w in LOCAL_ORDER:
                if pool[sku].get(w, 0) >= qty:
                    pool[sku][w] -= qty
                    decisions.append({**l, "motivo": "stock local", "destino": w})
                    placed_local = True
                    break
            if placed_local:
                continue
            # No se puede completar ni en D10 ni en ningun almacen propio. Solo en este
            # caso se mantiene/asigna a D10: si hay una compra en camino (incoming, que
            # segun §4 siempre aterriza en D10), la linea espera esa llegada.
            if incoming_dhl.get(sku, 0) >= qty:
                incoming_dhl[sku] -= qty
                decisions.append({**l, "motivo": "llega a D10", "destino": "D10"})
                continue
            total_local = sum(pool[sku].get(w, 0) for w in LOCAL_ORDER) if sku in pool else 0
            total_all = total_local + dhl_available.get(sku, 0)
            if total_all <= 0 and incoming_dhl.get(sku, 0) <= 0:
                decisions.append({**l, "motivo": "rotura", "destino": "D10"})
            else:
                decisions.append({**l, "motivo": "cola", "destino": "D10"})
            continue

        placed = False
        # Estabilidad (hysteresis): si la linea YA esta fisicamente comprometida
        # en un almacen local (Terrassa/Matadepera/AP), no la re-peleamos contra
        # el pool FIFO de stock libre en esta vuelta. Ese stock ya esta reservado
        # para este pedido en Shopify (no cuenta como "available" para nadie mas),
        # asi que reevaluarla cada ronda solo la hacia oscilar entre almacenes
        # cuando dos pedidos competian por el mismo SKU muy ajustado.
        # v72 (27/09/2026, decision de Bruno): esta hysteresis NO se toca a proposito —
        # un pedido que hoy sale bien de Terrassa con stock real se queda en Terrassa.
        # La baja de volumen de Terrassa es gradual (noche a noche, con la demanda
        # nueva), no un barrido forzado que generaria un montón de moves de golpe —
        # eso violaria el otro pedido de Bruno de minimizar moves/DIVIDIDO.
        cur_loc = l["fo_location"]
        if cur_loc in LOCAL_ORDER:
            # Antes de aceptar la hysteresis a ciegas, confirmar que el almacen
            # donde esta comprometida la linea tiene stock FISICO real para
            # cubrir todo lo comprometido ahi (available crudo, sin colchon,
            # >= 0). Si available crudo es negativo, el almacen esta
            # sobrevendido (on_hand no alcanza para lo que dice tener
            # comprometido, ni siquiera para esta misma linea) — igual que se
            # corrigio para "ya en D10" (ver §5/§9.23), no tiene sentido dejar
            # la linea fija en un almacen que no puede completarla de verdad.
            # Si available crudo es >= 0, el almacen SI cubre todo lo suyo
            # (incluida esta linea) y se mantiene sin re-pelear contra el pool.
            if raw_available.get(sku, {}).get(cur_loc, 0) >= 0:
                decisions.append({**l, "motivo": "stock local", "destino": cur_loc, "kept": True})
                placed = True
            else:
                # Hysteresis rota (almacen sobrevendido): se re-decide desde cero, con
                # la prioridad NUEVA de v72 (D10 primero, ver mas abajo).
                if dhl_available.get(sku, 0) >= qty:
                    dhl_available[sku] -= qty
                    decisions.append({**l, "motivo": "stock D10", "destino": "D10"})
                    placed = True
                if not placed:
                    for w in LOCAL_ORDER:
                        if pool[sku].get(w, 0) >= qty:
                            pool[sku][w] -= qty
                            decisions.append({**l, "motivo": "stock local", "destino": w})
                            placed = True
                            break
        if not placed:
            # CAMBIO PRINCIPAL DE v72 (27/09/2026, decision de Bruno en vivo): D10 se
            # prueba ANTES que los almacenes propios para toda demanda NUEVA (linea que
            # no tiene ya un almacen local fijo por hysteresis). Antes se probaba
            # LOCAL_ORDER completo (Terrassa primero) y solo se caia a D10 si ningun
            # almacen propio alcanzaba. Ahora: D10 -> Matadepera -> Almacen Principal ->
            # Terrassa (Terrassa ultimo recurso, solo si D10 y los otros dos no tienen).
            if dhl_available.get(sku, 0) >= qty:
                dhl_available[sku] -= qty
                decisions.append({**l, "motivo": "stock D10", "destino": "D10"})
                placed = True
            if not placed:
                for w in LOCAL_ORDER:
                    if pool[sku].get(w, 0) >= qty:
                        pool[sku][w] -= qty
                        decisions.append({**l, "motivo": "stock local", "destino": w})
                        placed = True
                        break
        if placed:
            continue
        if incoming_dhl.get(sku, 0) >= qty:
            incoming_dhl[sku] -= qty
            decisions.append({**l, "motivo": "llega a D10", "destino": "D10"})
            continue
        # rotura vs cola
        total_local = sum(pool[sku].get(w, 0) for w in LOCAL_ORDER) if sku in pool else 0
        total_all = total_local + dhl_available.get(sku, 0)
        if total_all <= 0 and incoming_dhl.get(sku, 0) <= 0:
            decisions.append({**l, "motivo": "rotura", "destino": "D10"})
        else:
            decisions.append({**l, "motivo": "cola", "destino": "D10"})

    group = agrupar(decisions, despiece_lines)
    return {"group": group, "skip": False, "decisions": decisions, "despiece_lines": despiece_lines}

def agrupar(decisions, despiece_lines):
    """Grupo del motor a partir de las decisiones (v92: funcion propia; la usa tambien juntar_en_d10)."""
    motivos = set(d["motivo"] for d in decisions)
    destinos_local = set(d["destino"] for d in decisions if d["motivo"] == "stock local")

    group = None
    if motivos == {"rotura"}:
        group = "05_ROTURA"
    elif motivos == {"stock D10"}:
        group = "02_D10_ENTERO_SALE_YA"
    elif "cola" in motivos and "llega a D10" not in motivos and not destinos_local:
        group = "04_D10_ENTERO_COLA"
    elif ("llega a D10" in motivos) and not destinos_local:
        group = "03_D10_ENTERO_ESPERA_RESERVA"
    elif motivos == {"stock local"} and len(destinos_local) == 1:
        w = next(iter(destinos_local))
        group = {"Terrassa": "06_TERRASSA", "Matadepera": "07_MATADEPERA", "Almacén Principal": "08_AP"}[w]
    elif destinos_local and "rotura" in motivos:
        group = "01_SPLIT_D"
    elif destinos_local and "stock D10" in motivos and "llega a D10" not in motivos and "cola" not in motivos:
        group = "01_SPLIT_A"
    elif destinos_local and "llega a D10" in motivos:
        group = "01_SPLIT_B"
    elif destinos_local and "cola" in motivos:
        group = "01_SPLIT_C"
    elif len(destinos_local) > 1 and motivos == {"stock local"}:
        group = "01_SPLIT_E"
    else:
        # fallback catch: mixture with local + D10 variants combos not caught above
        if destinos_local and len(destinos_local) > 1:
            group = "01_SPLIT_E"
        elif destinos_local:
            group = "01_SPLIT_A"
        else:
            group = "03_D10_ENTERO_ESPERA_RESERVA"

    # Decision de Northdeco (26/08 noche, quinta revision): un pedido MIXTO (lineas
    # normales + al menos una linea despiece) se reporta/agrupa como "09_DESPIECE",
    # igual que un pedido 100% despiece — Northdeco lo quiere ver ahi, no repartido
    # entre los grupos de almacen segun donde caiga su parte normal. Esto es SOLO
    # una etiqueta de agrupacion/reporte: las `decisions` de la(s) linea(s) normal(es)
    # se calculan igual que siempre (arriba) y se devuelven tal cual, para que el
    # reparto de almacen y los tags de almacen/RESERVA/SIN STOCK de esa parte normal
    # se sigan aplicando sin cambios (ver diff.py: ya no se salta el procesamiento de
    # `decisions` solo por group=="09_DESPIECE" cuando SI hay decisions).
    if despiece_lines:
        group = "09_DESPIECE"

    return group


def juntar_en_d10(results, dhl_available):
    """v92 (06/10/2026, decision de Bruno, caso #s138524): si un pedido tiene algo ESPERANDO en D10
    (`llega a D10` o `cola`, sin `rotura`) y lineas en un almacen propio, cada linea local que tenga
    stock LIBRE en D10 (lo que sobra tras el reparto de la corrida: no se le quita a ningun otro
    pedido) se pasa a D10, para que el pedido salga junto de D10 cuando llegue lo que espera.
    Si solo algunas lineas caben, se mueven esas ("mover lo que se pueda"). Rompe a proposito la
    hysteresis de esas lineas. Orden: el mismo del reparto (Repo > B2B > resto, por createdAt).
    No toca pedidos con despiece (los gestiona A.7) ni SPLIT_D (rotura). Estable: la corrida
    siguiente ve el pedido entero en D10 y lo mantiene mientras D10 tenga on_hand."""
    juntados = []
    for r in results:
        if r.get("skip") or r.get("despiece_lines"):
            continue
        ds = r.get("decisions") or []
        motivos = set(d["motivo"] for d in ds)
        if "rotura" in motivos or not (motivos & {"llega a D10", "cola"}):
            continue
        movidas = []
        for d in ds:
            if d["motivo"] != "stock local":
                continue
            if dhl_available.get(d["sku"], 0) >= d["qty"]:
                dhl_available[d["sku"]] -= d["qty"]
                movidas.append(f'{d["sku"]} x{d["qty"]} {d["destino"]}->D10')
                d["destino"] = "D10"
                d["motivo"] = "stock D10"
                d["juntar_d10"] = True
                d.pop("kept", None)
        if movidas:
            antes = r["group"]
            r["group"] = agrupar(ds, r.get("despiece_lines"))
            r["juntar_d10"] = movidas
            juntados.append((r["order"]["name"], antes, r["group"], movidas))
    return juntados


def free_unpaid_reservations(orders, raw_available):
    # Shopify compromete (committed) el stock de un pedido al crearlo, sin importar
    # si esta pagado o no — por eso "available" ya sale mas bajo aunque el pedido
    # nunca llegue a pagarse. Decision de Northdeco: ese stock NO debe quedar
    # bloqueado para los clientes que si pagaron y compraron despues. Se calcula
    # cuanto tiene comprometido cada pedido no pagado (grupo 11) por SKU/almacen y
    # se le devuelve esa cantidad al available CRUDO (antes de colchon/piso en 0)
    # ANTES de repartir entre los pedidos pagados (que se siguen procesando por
    # orden de creacion, como siempre).
    # IMPORTANTE (fix v17.1, ver §9.25): esto se suma sobre raw_available, NUNCA
    # sobre el pool ya recortado por colchon/piso en 0 — sumarlo despues del piso
    # puede inventar stock que no existe fisicamente. Ejemplo real: Almacen
    # Principal con on_hand=0, committed=12 (6 de un pedido no pagado + 6 de
    # pagados), available=-12. Sumar los 6 liberados sobre el pool ya en 0 daba
    # pool=6 (fantasma, el almacen sigue con on_hand=0). Sumandolo sobre el crudo
    # da -12+6=-6, que sigue siendo negativo tras aplicar colchon/piso — correcto,
    # porque el almacen fisicamente no tiene stock ni para los pedidos pagados.
    for o in orders.values():
        fstatus = o.get("financial_status")
        if fstatus is None or fstatus in PAID_STATUSES:
            continue
        for fo in o["fos"]:
            if fo["status"] in ("CLOSED", "CANCELLED"):
                continue
            loc = fo["location"]
            if loc not in LOCAL_ORDER and loc != "D10":
                continue
            for l in fo["lines"]:
                qty = l.get("remainingQuantity") or 0
                if qty <= 0:
                    continue
                sku = l.get("sku")
                name = l.get("name")
                if is_placeholder_line(sku, name) or is_despiece(sku, name) or not sku:
                    continue
                raw_available[sku][loc] = raw_available[sku].get(loc, 0) + qty

def main():
    orders = parse_orders("orders_raw.jsonl")
    stock = parse_inventory("inventory_raw.jsonl")
    raw_available, incoming_dhl = build_raw_available(stock)
    free_unpaid_reservations(orders, raw_available)
    pool, dhl_available = compute_usable_pool(raw_available)
    dhl_on_hand = build_dhl_on_hand(stock)

    # Los pedidos repo (v89) y luego los de PRIORITY_TAGS (ej. "B2B Partnership") se
    # procesan primero — asi se quedan con cualquier pool limitado (local, D10, incoming)
    # por delante de pedidos mas viejos sin prioridad. Dentro de cada grupo (repo / B2B /
    # normal), se sigue procesando por fecha de creacion, como siempre (ver sort_key).
    ordered = sorted(orders.values(), key=sort_key)
    results = []
    for o in ordered:
        r = classify_order(o, pool, dhl_available, incoming_dhl, raw_available, dhl_on_hand)
        r["order"] = o
        results.append(r)

    # v92: juntar en D10 los pedidos que esperan algo en D10 (stock D10 que sobra tras el reparto)
    juntados = juntar_en_d10(results, dhl_available)

    counts = defaultdict(int)
    for r in results:
        counts[r["group"]] += 1

    total_scope = sum(v for k, v in counts.items() if k not in FUERA_DE_SCOPE)

    print("=== conteos por grupo ===")
    for k in sorted(counts.keys()):
        print(f"{k}: {counts[k]}")
    print("TOTAL scope (excl DESISTIMIENTO/FRAUDE_CONTRACARGO/NO_PENDING/ARCHIVADO/11_NO_PAGADO):", total_scope)
    print("DESISTIMIENTO:", counts.get("DESISTIMIENTO", 0))
    print("FRAUDE_CONTRACARGO (tag FRAUDE o CONTRACARGO, v90, no tocar):", counts.get("FRAUDE_CONTRACARGO", 0))
    print("ARCHIVADO (pedido cerrado/archivado en Shopify, no tocar):", counts.get("ARCHIVADO", 0))
    print("11_NO_PAGADO (no pagado aun, no tocar hasta que este PAID/PARTIALLY_REFUNDED):", counts.get("11_NO_PAGADO", 0))
    print("NO_PENDING (sin lineas pendientes / ya resueltos):", counts.get("NO_PENDING", 0))
    print("total orders parsed:", len(orders))
    print(f"JUNTAR EN D10 (v92): {len(juntados)} pedidos,", sum(len(j[3]) for j in juntados), "lineas:",
          ", ".join(j[0] for j in juntados[:60]))
    repos = [o["name"] for o in ordered if es_repo(o)]
    print(f"REPO (prioridad v89): {len(repos)}", ", ".join(repos[:50]))

    # dump results for later use
    import pickle
    with open("scope.pkl", "wb") as f:
        pickle.dump(results, f)

if __name__ == "__main__":
    main()
