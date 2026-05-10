#include <RadioLib.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define LORA_SCK   9
#define LORA_MISO  11
#define LORA_MOSI  10
#define LORA_CS    8
#define LORA_RST   12
#define LORA_DIO1  14
#define LORA_BUSY  13
#define VEXT_CTRL  36
#define OLED_SDA   17
#define OLED_SCL   18
#define OLED_RST   21

#define LORA_FREQ  869.618
#define LORA_BW    62.5
#define LORA_SF    8
#define LORA_CR    8
#define LORA_SYNC  0x12    // private LoRa sync word (0x34 = LoRaWAN)

Adafruit_SSD1306 display(128, 64, &Wire, OLED_RST);
SX1262 radio = new Module(LORA_CS, LORA_DIO1, LORA_RST, LORA_BUSY);

volatile bool rxFlag = false;
int packetCount = 0;

void IRAM_ATTR onReceive() { rxFlag = true; }

// Time on air in ms: approximate for SX1262
float timeOnAir(int payloadBytes) {
  float bw_hz = LORA_BW * 1000.0;
  float ts = pow(2.0, LORA_SF) / bw_hz;        // symbol duration
  float t_preamble = (8.0 + 4.25) * ts;
  float n_sym = 8.0 + max(0.0,
    ceil((8.0 * payloadBytes - 4.0 * LORA_SF + 28.0 + 16.0) /
         (4.0 * (LORA_SF))) * LORA_CR);
  return (t_preamble + n_sym * ts) * 1000.0;
}

void oledUpdate(int count, float rssi, float snr, const char* info) {
  display.clearDisplay();
  display.setTextSize(1);
  display.setTextColor(SSD1306_WHITE);
  display.setCursor(0, 0);
  display.printf("%.3fMHz SF%d", LORA_FREQ, LORA_SF);
  display.drawLine(0, 9, 127, 9, SSD1306_WHITE);
  display.setCursor(0, 12);
  display.printf("#%d", count);
  display.setCursor(0, 22);
  display.printf("RSSI %.1fdBm", rssi);
  display.setCursor(0, 32);
  display.printf("SNR  %.1fdB", snr);
  display.setCursor(0, 44);
  display.setTextSize(1);
  display.print(info);
  display.display();
}

void setup() {
  Serial.begin(115200);

  pinMode(VEXT_CTRL, OUTPUT);
  digitalWrite(VEXT_CTRL, LOW);
  delay(100);

  Wire.begin(OLED_SDA, OLED_SCL);
  if (display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
    display.clearDisplay();
    display.setTextSize(1);
    display.setTextColor(SSD1306_WHITE);
    display.setCursor(16, 20);
    display.print("LoRa Scanner");
    display.setCursor(22, 32);
    display.print("Starting...");
    display.display();
  }

  SPI.begin(LORA_SCK, LORA_MISO, LORA_MOSI, LORA_CS);
  int state = radio.begin(LORA_FREQ, LORA_BW, LORA_SF, LORA_CR);
  if (state != RADIOLIB_ERR_NONE) {
    Serial.printf("ERR|LoRa init failed: %d\n", state);
    while (true) delay(1000);
  }

  radio.setSyncWord(LORA_SYNC);
  radio.setDio1Action(onReceive);
  radio.startReceive();

  // Machine-readable config header
  Serial.printf("CFG|freq=%.3f|bw=%.1f|sf=%d|cr=4/%d|sync=0x%02X\n",
                LORA_FREQ, LORA_BW, LORA_SF, LORA_CR, LORA_SYNC);
}

void loop() {
  if (!rxFlag) return;
  rxFlag = false;

  uint32_t rx_ts = millis();
  uint8_t buf[256];
  int state = radio.readData(buf, 0);

  if (state == RADIOLIB_ERR_NONE) {
    int     len    = radio.getPacketLength();
    float   rssi   = radio.getRSSI();
    float   snr    = radio.getSNR();
    float   fe     = radio.getFrequencyError();   // Hz
    float   toa    = timeOnAir(len);
    packetCount++;

    // Hex payload
    char hex[513] = {0};
    for (int i = 0; i < min(len, 255); i++)
      sprintf(hex + i*2, "%02X", buf[i]);

    // ASCII preview
    char asc[33] = {0};
    for (int i = 0; i < min(len, 32); i++)
      asc[i] = (buf[i] >= 32 && buf[i] < 127) ? (char)buf[i] : '.';

    // LoRaWAN MHDR detection
    uint8_t mtype = 0xFF;
    char proto[16] = "UNKNOWN";
    char devaddr[12] = "N/A";
    if (len >= 5) {
      uint8_t mhdr = buf[0];
      mtype = (mhdr >> 5) & 0x07;
      const char* mtypes[] = {"JOIN_REQ","JOIN_ACC","UNCONF_UP","UNCONF_DN",
                               "CONF_UP","CONF_DN","LORAWAN","PROP"};
      if (mtype <= 7) {
        strncpy(proto, mtypes[mtype], 15);
        // DevAddr in bytes 1-4 for data frames (mtype 2-5)
        if (mtype >= 2 && mtype <= 5 && len >= 8) {
          sprintf(devaddr, "%02X%02X%02X%02X",
                  buf[4], buf[3], buf[2], buf[1]);
        }
      }
    }

    // ASCII last so any pipe chars in payload don't shift other fields
    Serial.printf(
      "PKT|%d|%lu|%.2f|%.2f|%.1f|%.2f|%d|%s|%s|%s|%s\n",
      packetCount,   // 1  packet index
      rx_ts,         // 2  timestamp ms
      rssi,          // 3  RSSI dBm
      snr,           // 4  SNR dB
      fe,            // 5  frequency error Hz
      toa,           // 6  time on air ms
      len,           // 7  payload length
      proto,         // 8  detected protocol
      devaddr,       // 9  DevAddr if LoRaWAN
      hex,           // 10 hex payload
      asc            // 11 ASCII preview — last, may contain '|'
    );

    oledUpdate(packetCount, rssi, snr, proto);

  } else if (state == RADIOLIB_ERR_CRC_MISMATCH) {
    Serial.printf("CRC|%lu|crc_error\n", millis());
  }

  radio.startReceive();
}
