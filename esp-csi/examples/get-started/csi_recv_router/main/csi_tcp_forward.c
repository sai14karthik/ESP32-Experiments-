/*
 * TCP client forwarder for CSI_DATA lines.
 */
#include "csi_tcp_forward.h"

#include <stdlib.h>
#include <string.h>
#include <errno.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "lwip/sockets.h"
#include "lwip/netdb.h"
#include "sdkconfig.h"

static const char *TAG = "csi_tcp";

#if CONFIG_CSI_TCP_ENABLE

#define CSI_TCP_LINE_MAX    4096
#define CSI_TCP_QUEUE_LEN   64
#define CSI_TCP_RECONNECT_MS 2000

typedef struct {
    uint16_t len;
    char *data; /* heap; freed by TCP task after send (or drop) */
} csi_tcp_msg_t;

static QueueHandle_t s_q;
static int s_sock = -1;

bool csi_tcp_forward_enabled(void)
{
    return true;
}

void csi_tcp_forward_enqueue(const char *line, size_t line_len)
{
    if (!s_q || !line || line_len == 0) {
        return;
    }
    if (line_len >= CSI_TCP_LINE_MAX) {
        line_len = CSI_TCP_LINE_MAX - 1;
    }
    char *copy = malloc(line_len + 1);
    if (!copy) {
        return;
    }
    memcpy(copy, line, line_len);
    copy[line_len] = '\0';

    csi_tcp_msg_t msg = {
        .len = (uint16_t)line_len,
        .data = copy,
    };
    if (xQueueSend(s_q, &msg, 0) != pdTRUE) {
        free(copy); /* Drop rather than block Wi-Fi CSI callback. */
    }
}

static void csi_tcp_close_sock(void)
{
    if (s_sock >= 0) {
        shutdown(s_sock, SHUT_RDWR);
        close(s_sock);
        s_sock = -1;
    }
}

static bool csi_tcp_connect(void)
{
    const char *host = CONFIG_CSI_TCP_HOST;
    const int port = CONFIG_CSI_TCP_PORT;

    struct sockaddr_in dest = {0};
    dest.sin_family = AF_INET;
    dest.sin_port = htons(port);
    if (inet_aton(host, &dest.sin_addr) == 0) {
        ESP_LOGE(TAG, "invalid host IP: %s", host);
        return false;
    }

    int sock = socket(AF_INET, SOCK_STREAM, IPPROTO_IP);
    if (sock < 0) {
        ESP_LOGE(TAG, "socket(): errno %d", errno);
        return false;
    }

    /* No send/recv deadline: brief Mini load must not tear the socket. */
    struct timeval tv = {.tv_sec = 0, .tv_usec = 0};
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(sock, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));

    /* Keepalive OFF — CSI stream is the liveness probe. Aggressive KA caused ~1min reconnect storms. */
    int ka = 0;
    setsockopt(sock, SOL_SOCKET, SO_KEEPALIVE, &ka, sizeof(ka));

    if (connect(sock, (struct sockaddr *)&dest, sizeof(dest)) != 0) {
        ESP_LOGW(TAG, "connect %s:%d failed errno=%d", host, port, errno);
        close(sock);
        return false;
    }

    s_sock = sock;
    ESP_LOGI(TAG, "connected to ingest %s:%d", host, port);
    return true;
}

static bool csi_tcp_send_all(const char *data, size_t len)
{
    size_t sent = 0;
    int spins = 0;
    while (sent < len) {
        int n = send(s_sock, data + sent, len - sent, 0);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            if ((errno == EAGAIN || errno == EWOULDBLOCK) && spins++ < 50) {
                vTaskDelay(pdMS_TO_TICKS(20));
                continue;
            }
            return false;
        }
        if (n == 0) {
            return false;
        }
        spins = 0;
        sent += (size_t)n;
    }
    return true;
}

static void csi_tcp_task(void *arg)
{
    (void)arg;
    ESP_LOGI(TAG, "forwarder task up → %s:%d", CONFIG_CSI_TCP_HOST, CONFIG_CSI_TCP_PORT);

    csi_tcp_msg_t msg;
    while (true) {
        if (s_sock < 0) {
            if (!csi_tcp_connect()) {
                vTaskDelay(pdMS_TO_TICKS(CSI_TCP_RECONNECT_MS));
                continue;
            }
        }

        if (xQueueReceive(s_q, &msg, pdMS_TO_TICKS(1000)) != pdTRUE) {
            continue;
        }

        bool ok = csi_tcp_send_all(msg.data, msg.len);
        if (!ok) {
            /* One quiet retry before tearing the socket down. */
            vTaskDelay(pdMS_TO_TICKS(100));
            ok = csi_tcp_send_all(msg.data, msg.len);
        }
        if (!ok) {
            ESP_LOGW(TAG, "send failed; reconnecting");
            csi_tcp_close_sock();
            if (xQueueSendToFront(s_q, &msg, 0) != pdTRUE) {
                free(msg.data);
            }
            vTaskDelay(pdMS_TO_TICKS(CSI_TCP_RECONNECT_MS));
            continue;
        }
        free(msg.data);
    }
}

void csi_tcp_forward_start(void)
{
    if (s_q) {
        return;
    }
    s_q = xQueueCreate(CSI_TCP_QUEUE_LEN, sizeof(csi_tcp_msg_t));
    if (!s_q) {
        ESP_LOGE(TAG, "queue create failed");
        return;
    }
    /* Small stack: messages live on heap, not on this task stack. */
    BaseType_t ok = xTaskCreate(csi_tcp_task, "csi_tcp", 4096, NULL, 5, NULL);
    if (ok != pdPASS) {
        ESP_LOGE(TAG, "task create failed");
    }
}

void csi_tcp_forward_force_reconnect(void)
{
    ESP_LOGI(TAG, "force TCP reconnect");
    csi_tcp_close_sock();
}

#else /* !CONFIG_CSI_TCP_ENABLE */

bool csi_tcp_forward_enabled(void)
{
    return false;
}

void csi_tcp_forward_enqueue(const char *line, size_t line_len)
{
    (void)line;
    (void)line_len;
}

void csi_tcp_forward_start(void)
{
}

void csi_tcp_forward_force_reconnect(void)
{
}

#endif
