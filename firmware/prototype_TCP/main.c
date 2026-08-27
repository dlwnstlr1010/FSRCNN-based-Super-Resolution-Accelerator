/*
 * main.c - TCP RX -> DMA -> TX pipeline
 */

#include "xparameters.h"
#include "xil_printf.h"
#include "xil_cache.h"
#include "xaxidma.h"
#include "sleep.h"

#include "netif/xadapter.h"
#include "lwip/init.h"
#include "lwip/tcp.h"
#include "platform.h"


extern struct netif echo_netif;
extern struct tcp_pcb *client_pcb;
extern int start_application(void);
extern u8* tcp_rx_peek_frame(int *idx_out);
extern int  tcp_rx_pop_frame(void);
extern int start_sending(const u8 *buf, u32 len);  // async TX kick (echo.c)
extern int tcp_tx_is_busy(void);
extern int  transfer_data(const u8 *data, u32 len);   // from echo.c

#define IN_IMG_W   320
#define IN_IMG_H   180
#define IN_BPP     4
#define IN_FRAME_BYTES (IN_IMG_W * IN_IMG_H * IN_BPP)

#define OUT_IMG_W  1280
#define OUT_IMG_H  720
#define OUT_BPP    1
#define OUT_FRAME_BYTES (OUT_IMG_W * OUT_IMG_H * OUT_BPP)

/* Double output buffers */
static u8 out_buf0[OUT_FRAME_BYTES] __attribute__((aligned(64)));
static u8 out_buf1[OUT_FRAME_BYTES] __attribute__((aligned(64)));
static u8 *cur_out = out_buf0;

/* DMA and DMA configuration */
XAxiDma myDma;
XAxiDma_Config *myDmaConfig;

/* DMA checkHalted function declare */
u32 checkHalted(u32 baseAddress, u32 offset) {
    return (XAxiDma_ReadReg(baseAddress, offset)) & XAXIDMA_HALTED_MASK;
}
/* Process one frame: in_ptr -> PL -> out_ptr */
static int process_one_frame(const u8 *in_ptr, u8 *out_ptr)
{
    /* Cache ops */
    Xil_DCacheFlushRange((INTPTR)in_ptr, IN_FRAME_BYTES);
    Xil_DCacheInvalidateRange((INTPTR)out_ptr, OUT_FRAME_BYTES);

    /* Kick DMA: S2MM first, then MM2S */
    u32 s2mm = XAxiDma_SimpleTransfer(&myDma,
                    (UINTPTR)out_ptr, OUT_FRAME_BYTES, XAXIDMA_DEVICE_TO_DMA);
    u32 mm2s = XAxiDma_SimpleTransfer(&myDma,
                    (UINTPTR)in_ptr,  IN_FRAME_BYTES,  XAXIDMA_DMA_TO_DEVICE);
    if (s2mm != XST_SUCCESS || mm2s != XST_SUCCESS) {
       xil_printf("[ERROR] DMA transfer submission failed (s2mm=%d, mm2s=%d)\n\r",
               s2mm, mm2s);
       return -1;
    }

    /* Wait for DMA completion */
    int timeout = 0;
    while (XAxiDma_Busy(&myDma, XAXIDMA_DEVICE_TO_DMA)){
       if (++timeout > 100000000) {
         xil_printf("[ERROR] S2MM timeout!\n\r");
         return -2;
      }
    };
    timeout = 0;
    while (XAxiDma_Busy(&myDma, XAXIDMA_DMA_TO_DEVICE)){
       if (++timeout > 100000000) {
         xil_printf("[ERROR] MM2S timeout!\n\r");
         return -2;
      }
    };

     Xil_DCacheInvalidateRange((INTPTR)out_ptr, OUT_FRAME_BYTES);

    return 0;
}

int main()
{
   ip_addr_t ipaddr, netmask, gw;
   unsigned char mac[6] = { 0x00, 0x0a, 0x35, 0x00, 0x01, 0x02 };
   u32 status;

    init_platform();

    IP4_ADDR(&ipaddr, 192,168,1,20);
    IP4_ADDR(&netmask,255,255,255,0);
    IP4_ADDR(&gw,     192,168,1,1);

    //xil_printf("[MAIN] lwIP init...\n\r");
    lwip_init();

    //xil_printf("[MAIN] Add network interface...\n\r");
    if (!xemac_add(&echo_netif, &ipaddr, &netmask, &gw,
                mac, XPAR_PSU_ETHERNET_3_BASEADDR)) {
        xil_printf("[ERROR] NETIF add failed\r\n");
        return -1;
    }

    netif_set_default(&echo_netif);
    platform_enable_interrupts();
    netif_set_up(&echo_netif);

    // DMA initialization
    myDmaConfig = XAxiDma_LookupConfigBaseAddr(XPAR_AXI_DMA_0_BASEADDR);
   status = XAxiDma_CfgInitialize(&myDma, myDmaConfig);
   if (status != XST_SUCCESS) {
      xil_printf("DMA initialization failed\r\n");
      return -1;
   }
   xil_printf("DMA initialization success..\r\n");
   status = checkHalted(XPAR_AXI_DMA_0_BASEADDR, 0x4);
   xil_printf("Status before data transfer: %0x\r\n", status);

   if (start_application() != 0) {
           xil_printf("[ERROR] start_application failed\r\n");
           return -2;
       }

   // Wait until a client connects
    xil_printf("Waiting for client connection...\n\r");
    while (client_pcb == NULL) {
        xemacif_input(&echo_netif);  // Process incoming packets
        usleep(1000);                 // Small delay to avoid busy-wait
    }

    xil_printf("[MAIN] Client connected. Starting TCP transfer...\n\r");

    while (1) {
        /* Pump lwIP input */
        xemacif_input(&echo_netif);

        /* Process one frame if available */
        if (!tcp_tx_is_busy()) {
            int idx = -1;
            u8 *in_ptr = tcp_rx_peek_frame(&idx);
            if (in_ptr) {
                //xil_printf("[MAIN] Frame %d received, processing...\n\r", idx);
                if (process_one_frame(in_ptr, cur_out) == 0) {
                    //xil_printf("[MAIN] DMA done, sending result...\n\r");
                    int tx_res = start_sending(cur_out, OUT_FRAME_BYTES);
                    if (tx_res == 0){
                        //xil_printf("[MAIN] TX success\n\r");
                       tcp_rx_pop_frame();     // release RX frame
                    }
                    else
                        //xil_printf("[WARN] TX incomplete (res=%d)\n\r", tx_res);
                    cur_out = (cur_out == out_buf0) ? out_buf1 : out_buf0;
                }
            }
        }
    }

    cleanup_platform();
    return 0;
}
