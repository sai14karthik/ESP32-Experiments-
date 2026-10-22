#include "lab_sta.h"

#include <string.h>
#include <errno.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "esp_now.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

#include "lab_wifi.local.h"

static const char *TAG = "lab_sta";
static EventGroupHandle_t s_wifi;
static const int CONNECTED_BIT = BIT0;

static void lab_wifi_event(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        xEventGroupClearBits(s_wifi, CONNECTED_BIT);
        esp_wifi_connect();
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "lab Wi-Fi up  ip=" IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(s_wifi, CONNECTED_BIT);
    }
}

esp_err_t lab_sta_connect(void)
{
    if (!esp_netif_get_handle_from_ifkey("WIFI_STA_DEF")) {
        esp_netif_create_default_wifi_sta();
    }
    if (!s_wifi) {
        s_wifi = xEventGroupCreate();
    }

    static bool s_handler;
    if (!s_handler) {
        ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED, lab_wifi_event, NULL));
        ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, lab_wifi_event, NULL));
        s_handler = true;
    }

    wifi_config_t sta = {0};
    strncpy((char *)sta.sta.ssid, LAB_WIFI_SSID, sizeof(sta.sta.ssid) - 1);
    strncpy((char *)sta.sta.password, LAB_WIFI_PASS, sizeof(sta.sta.password) - 1);
    sta.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;
    sta.sta.pmf_cfg.capable = true;
    sta.sta.pmf_cfg.required = false;
#ifdef LAB_WIFI_BSSID
    /* One access point for every board. Same SSID has several radios. */
    {
        uint8_t bssid[6] = LAB_WIFI_BSSID;
        memcpy(sta.sta.bssid, bssid, 6);
    }
    sta.sta.bssid_set = true;
    sta.sta.channel = LAB_WIFI_CHANNEL;
    sta.sta.scan_method = WIFI_FAST_SCAN;
    ESP_LOGI(TAG, "joining %s  locked to " MACSTR " channel %d",
             LAB_WIFI_SSID, MAC2STR(sta.sta.bssid), LAB_WIFI_CHANNEL);
#else
    sta.sta.scan_method = WIFI_ALL_CHANNEL_SCAN;
    ESP_LOGI(TAG, "joining %s", LAB_WIFI_SSID);
#endif

    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &sta));
    esp_err_t err = esp_wifi_connect();
    if (err != ESP_OK && err != ESP_ERR_WIFI_CONN) {
        ESP_LOGE(TAG, "connect failed: %s", esp_err_to_name(err));
        return err;
    }

    EventBits_t bits = xEventGroupWaitBits(s_wifi, CONNECTED_BIT, pdFALSE, pdTRUE, pdMS_TO_TICKS(30000));
    if ((bits & CONNECTED_BIT) == 0) {
        ESP_LOGW(TAG, "no IP from %s yet — still retrying", LAB_WIFI_SSID);
        return ESP_ERR_TIMEOUT;
    }

    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));
    uint8_t primary = 0;
    wifi_second_chan_t second = WIFI_SECOND_CHAN_NONE;
    ESP_ERROR_CHECK(esp_wifi_get_channel(&primary, &second));
    ESP_LOGI(TAG, "radio channel %u (ESP-NOW follows this)", primary);
    return ESP_OK;
}

void lab_espnow_use_current_channel(const uint8_t mac[6])
{
    esp_now_del_peer(mac);
    esp_now_peer_info_t peer = {
        .channel = 0,
        .ifidx = WIFI_IF_STA,
        .encrypt = false,
    };
    memcpy(peer.peer_addr, mac, 6);
    esp_err_t err = esp_now_add_peer(&peer);
    if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
        ESP_LOGW(TAG, "ESP-NOW peer refresh failed: %s", esp_err_to_name(err));
    }
}

static int s_sock = -1;

static void lab_close_sock(void)
{
    if (s_sock >= 0) {
        shutdown(s_sock, SHUT_RDWR);
        close(s_sock);
        s_sock = -1;
    }
}

static bool lab_tcp_connect(void)
{
    struct sockaddr_in dest = {0};
    dest.sin_family = AF_INET;
    dest.sin_port = htons(LAB_MINI_PORT);
    if (inet_aton(LAB_MINI_HOST, &dest.sin_addr) == 0) {
        ESP_LOGE(TAG, "bad Mini host");
        return false;
    }
    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (sock < 0) {
        return false;
    }
    struct timeval tv = {.tv_sec = 5, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    if (connect(sock, (struct sockaddr *)&dest, sizeof(dest)) != 0) {
        ESP_LOGW(TAG, "Mini %s:%d unreachable errno=%d", LAB_MINI_HOST, LAB_MINI_PORT, errno);
        close(sock);
        return false;
    }
    s_sock = sock;
    ESP_LOGI(TAG, "pushing status to Mini %s:%d", LAB_MINI_HOST, LAB_MINI_PORT);
    return true;
}

static void lab_report_task(void *arg)
{
    lab_report_fill_fn fill = ((void **)arg)[0];
    void *ctx = ((void **)arg)[1];
    char line[768];

    while (true) {
        EventBits_t bits = s_wifi ? xEventGroupGetBits(s_wifi) : 0;
        if ((bits & CONNECTED_BIT) == 0) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        if (s_sock < 0 && !lab_tcp_connect()) {
            vTaskDelay(pdMS_TO_TICKS(2000));
            continue;
        }
        fill(line, sizeof(line), ctx);
        size_t len = strnlen(line, sizeof(line) - 2);
        if (len == 0 || line[len - 1] != '\n') {
            line[len++] = '\n';
            line[len] = '\0';
        }
        size_t sent = 0;
        while (sent < len) {
            int n = send(s_sock, line + sent, len - sent, 0);
            if (n < 0) {
                if (errno == EINTR) {
                    continue;
                }
                ESP_LOGW(TAG, "send failed errno=%d", errno);
                lab_close_sock();
                break;
            }
            if (n == 0) {
                lab_close_sock();
                break;
            }
            sent += (size_t)n;
        }
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}

void lab_report_start(lab_report_fill_fn fill, void *ctx)
{
    static void *args[2];
    args[0] = (void *)fill;
    args[1] = ctx;
    if (xTaskCreate(lab_report_task, "lab_report", 6144, args, 4, NULL) != pdPASS) {
        ESP_LOGE(TAG, "report task failed");
    }
}
