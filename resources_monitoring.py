import psutil
import time
import subprocess

# Константы
MAX_BLOCK_STORAGE_GB = 147.12
MAX_OUTBOUND_TRAFFIC_TB = 10

def get_system_info():
    # Получаем информацию о системе с помощью команды 'w'
    result = subprocess.run(['w', '-i'], capture_output=True, text=True)
    lines = result.stdout.split('\n')
    system_load = float(lines[0].split(':')[1].split(',')[0].strip())
    return {
        'system_load': system_load
    }

def get_disk_usage():
    usage = psutil.disk_usage('/')
    return usage.percent

def get_memory_usage():
    memory = psutil.virtual_memory()
    return memory.percent

def get_network_usage(interface):
    net_io = psutil.net_io_counters(pernic=True)
    if interface in net_io:
        return net_io[interface].bytes_sent / (1024 * 1024 * 1024)  # Convert to GB
    return 0

def monitor_resources():
    system_info = get_system_info()
    disk_usage = get_disk_usage()
    memory_usage = get_memory_usage()
    
    # Мониторинг обоих интерфейсов
    enp0s6_traffic = get_network_usage('enp0s6')
    xray_traffic = get_network_usage('xray')
    total_outbound_traffic = enp0s6_traffic + xray_traffic
    
    return {
        "system_load": system_info['system_load'],
        "disk_usage": disk_usage,
        "memory_usage": memory_usage,
        "outbound_traffic_percent": (total_outbound_traffic / (MAX_OUTBOUND_TRAFFIC_TB * 1024)) * 100
    }

if __name__ == "__main__":
    usage = monitor_resources()
    print(f"System Load: {usage['system_load']}")
    print(f"Disk Usage: {usage['disk_usage']:.2f}% of {MAX_BLOCK_STORAGE_GB} GB")
    print(f"Memory Usage: {usage['memory_usage']:.2f}%")
    print(f"Outbound Traffic: {usage['outbound_traffic_percent']:.2f}% of {MAX_OUTBOUND_TRAFFIC_TB} TB")
