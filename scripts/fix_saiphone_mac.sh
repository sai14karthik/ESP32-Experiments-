
set -euo pipefail

ESP_IP="${1:-172.20.10.3}"
MAC_IP="${2:-172.20.10.2}"

networksetup -setmanual "Wi-Fi" "$MAC_IP" 255.255.255.240 172.20.10.1

sleep 2
echo "Mac en0:" >&2
ifconfig en0 | grep "inet " || true
echo "Ping ESP $ESP_IP:" >&2
ping -c 2 -W 2 "$ESP_IP" || true
echo "HTTP test:" >&2
curl -s -o /dev/null -w "  http://${ESP_IP}/ -> %{http_code}\n" --connect-timeout 5 "http://${ESP_IP}/" || true
curl -s -o /dev/null -w "  http://${ESP_IP}:81/stream -> %{http_code}\n" --connect-timeout 5 "http://${ESP_IP}:81/stream" || true
