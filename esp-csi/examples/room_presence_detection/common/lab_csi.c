#include "lab_sta.h"

#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_mac.h"
#include "esp_radar.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include "lwip/inet.h"
#include "lwip/sockets.h"

#include "lab_wifi.local.h"

static const char *TAG = "lab_csi";

/* Full HT-LTF. HT20 is 112 bytes; HT40 is 224. A short copy is not usable CSI. */
#define LAB_CSI_MAX 224

typedef struct {
    uint32_t seq;
    uint8_t mac[6];
    int8_t rssi;
    uint8_t channel;
    uint16_t n;
    int16_t iq[LAB_CSI_MAX];
} lab_csi_pkt_t;

static QueueHandle_t s_csi_q;
static uint8_t s_csi_node;
static int s_csi_sock = -1;

void lab_csi_filtered_cb(void *ctx, const void *info_void)
{
    const wifi_csi_filtered_info_t *info = info_void;
    (void)ctx;
    if (s_csi_q == NULL || info == NULL || info->valid_len == 0) {
        return;
    }
    lab_csi_pkt_t *pkt = calloc(1, sizeof(*pkt));
    if (pkt == NULL) {
        return;
    }
    /* Presence uses HT-LTF only. valid_data is LLTF then HT-LTF; copying
     * from the start would store the wrong field and can cut the real one. */
    uint16_t byte_off = info->valid_lltf_len;
    uint16_t byte_len = info->valid_ht_ltf_len;
    if (byte_len == 0 || (byte_len % 2) != 0) {
        free(pkt);
        return;
    }
    const uint8_t *base = (const uint8_t *)info->valid_data;
    uint16_t count;
    if (info->data_type == WIFI_CSI_DATA_TYPE_INT16) {
        count = byte_len / 2;
        if (count == 0 || count > LAB_CSI_MAX) {
            free(pkt);
            return;
        }
        const int16_t *src = (const int16_t *)(base + byte_off);
        for (uint16_t i = 0; i < count; i++) {
            pkt->iq[i] = src[i];
        }
    } else {
        count = byte_len;
        if (count > LAB_CSI_MAX) {
            free(pkt);
            return;
        }
        const int8_t *src = (const int8_t *)(base + byte_off);
        for (uint16_t i = 0; i < count; i++) {
            pkt->iq[i] = src[i];
        }
    }
    pkt->seq = info->seq_id;
    memcpy(pkt->mac, info->mac, 6);
    pkt->rssi = info->rx_ctrl_info.rssi;
    pkt->channel = info->rx_ctrl_info.channel;
    pkt->n = count;
    if (xQueueSend(s_csi_q, &pkt, 0) != pdTRUE) {
        free(pkt);
    }
}

static void lab_csi_close(void)
{
    if (s_csi_sock >= 0) {
        shutdown(s_csi_sock, SHUT_RDWR);
        close(s_csi_sock);
        s_csi_sock = -1;
    }
}

static bool lab_csi_connect(void)
{
    struct sockaddr_in dest = {0};
    dest.sin_family = AF_INET;
    dest.sin_port = htons(LAB_MINI_CSI_PORT);
    if (inet_aton(LAB_MINI_HOST, &dest.sin_addr) == 0) {
        return false;
    }
    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (sock < 0) {
        return false;
    }
    struct timeval tv = {.tv_sec = 2, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    if (connect(sock, (struct sockaddr *)&dest, sizeof(dest)) != 0) {
        ESP_LOGW(TAG, "Mini CSI %s:%d unreachable errno=%d", LAB_MINI_HOST, LAB_MINI_CSI_PORT, errno);
        close(sock);
        return false;
    }
    s_csi_sock = sock;
    ESP_LOGI(TAG, "pushing CSI node %u to Mini %s:%d", s_csi_node, LAB_MINI_HOST, LAB_MINI_CSI_PORT);
    return true;
}

static void lab_csi_task(void *arg)
{
    (void)arg;
    char *line = malloc(4096);
    if (line == NULL) {
        ESP_LOGE(TAG, "CSI line buffer failed");
        vTaskDelete(NULL);
        return;
    }
    uint32_t sent_n = 0;
    while (true) {
        if (!lab_wifi_ready()) {
            vTaskDelay(pdMS_TO_TICKS(500));
            continue;
        }
        lab_csi_pkt_t *pkt = NULL;
        if (xQueueReceive(s_csi_q, &pkt, pdMS_TO_TICKS(500)) != pdTRUE) {
            continue;
        }
        if (s_csi_sock < 0 && !lab_csi_connect()) {
            free(pkt);
            vTaskDelay(pdMS_TO_TICKS(1000));
            continue;
        }
        uint16_t iq_n = pkt->n;
        int off = snprintf(line, 4096, "CSI,%u,%lu," MACSTR ",%d,%u,ht,%u",
                           s_csi_node, (unsigned long)pkt->seq, MAC2STR(pkt->mac),
                           pkt->rssi, pkt->channel, pkt->n);
        uint16_t wrote = 0;
        for (uint16_t i = 0; i < pkt->n && off > 0 && off < 4000; i++) {
            int add = snprintf(line + off, 4096 - (size_t)off, ",%d", pkt->iq[i]);
            if (add < 0 || off + add >= 4000) {
                off = -1;
                break;
            }
            off += add;
            wrote++;
        }
        free(pkt);
        if (off <= 0 || wrote != iq_n) {
            continue;
        }
        line[off++] = '\n';
        size_t sent = 0;
        bool ok = true;
        while (sent < (size_t)off) {
            int n = send(s_csi_sock, line + sent, (size_t)off - sent, 0);
            if (n < 0) {
                if (errno == EINTR) {
                    continue;
                }
                lab_csi_close();
                ok = false;
                break;
            }
            if (n == 0) {
                lab_csi_close();
                ok = false;
                break;
            }
            sent += (size_t)n;
        }
        if (!ok) {
            continue;
        }
        sent_n++;
        if ((sent_n % 100) == 1) {
            ESP_LOGI(TAG, "CSI forwarded %lu node %u ht-ltf samples %u",
                     (unsigned long)sent_n, s_csi_node, iq_n);
        }
    }
}

void lab_csi_start(uint8_t node_id)
{
    s_csi_node = node_id;
    if (s_csi_q != NULL) {
        return;
    }
    s_csi_q = xQueueCreate(24, sizeof(lab_csi_pkt_t *));
    if (s_csi_q == NULL || xTaskCreate(lab_csi_task, "lab_csi", 8192, NULL, 3, NULL) != pdPASS) {
        ESP_LOGE(TAG, "CSI forward task failed");
    }
}
