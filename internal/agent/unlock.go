package agent

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Media unlock statuses (same wording as the original Python probe).
const (
	StatusUnlocked = "解锁"
	StatusCN       = "送中"
	StatusFailed   = "失败"
	StatusUnknown  = "未知"
	StatusNA       = "N/A"
)

type MediaResult struct {
	Status string `json:"status"`
	Region string `json:"region"`
}

// UnlockResult is returned to Master as JSON by /trigger_quality_sync.
type UnlockResult struct {
	Node    string      `json:"node"`
	Alias   string      `json:"alias"`
	IP      string      `json:"ip"`
	Country string      `json:"country"`
	Org     string      `json:"org"`
	YT      MediaResult `json:"yt"`
	Play    MediaResult `json:"play"`
	Gemini  MediaResult `json:"gemini"`
	Port25  string      `json:"port25"` // open / blocked / untested
	Error   string      `json:"error,omitempty"`
}

const mediaTimeout = 10 * time.Second

var (
	ytGLRe         = regexp.MustCompile(`"INNERTUBE_CONTEXT_GL":"([A-Z]{2})"`)
	playRegionRe   = regexp.MustCompile(`"zQmIje":"([A-Z]{2})"`)
	geminiRegionRe = regexp.MustCompile(`2,1,200,"([A-Z]{3})"`)
	ipLikeRe       = regexp.MustCompile(`^[\d.a-fA-F:\[\]]+$`)
)

const (
	ytUA     = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
	ytCookie = "YSC=BiCUU3-5Gdk; CONSENT=YES+cb.20220301-11-p0.en+FX+700; GPS=1; VISITOR_INFO1_LIVE=4VwPMkB7W5A; PREF=tz=Asia.Shanghai; _gcl_au=1.1.1809531354.1646633279"
	playUA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"

	geminiCookie = "__Secure-BUCKET=CIEC; SEARCH_SAMESITE=CgQIzaAB; AEC=AaJma5soTV08YUyGfUjJv1NkOxzb7GCpo6xwndB3lQdzz2SJBP8bSzZ2GHs; " +
		"__utmzz=utmcsr=(direct)|utmcmd=(none)|utmccn=(direct); __utmzzses=1; " +
		"NID=530=EMM0EWKz1n0hP2LjRfTdlHSO9WdulkPeZwivqZLlDOtpw12QfNYYLyobn9ZYRVbqAnsIkwniZeDoI_yp_WMVmhUsUA6HblyabKKyC5TYzfmoLr-KwvGUpiUZL-YKkU9DTAujsPLmIZBEYMmgt3q8RFz3ZCTU81LKBxq4pViMaXhtnj0GZUb_7Br3wTxkE7RSn8NFRboQKlQPT3v7DXeey9TJ8kUWMRpQRcM_UK_-7kBrOE8ofUS2r9q54l3elhP2As-z4GfBFKOzqZdGZCa6ojZBxvIR0F8zBVgmTx2vqGEKVfK3XEIkLq8sEXL3RRzJPSOVMS5BCPRF_E7rZgqOKB32jn_BCBo5-X-qeycDrORCihcbspdsjR2U5vWDe6zUXrHCI0In3ezxQbs_nffyV5BPxTcfw-lXIlECO7rm5HHpy13sDRl75mrOBKslC9UAAqRPnpL-voKIrhucOvUz"
)

var ytHeaders = map[string]string{
	"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
}

var playHeaders = map[string]string{
	"Accept":                      "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
	"Accept-Language":             "zh-CN,zh;q=0.9",
	"Cache-Control":               "no-cache",
	"Pragma":                      "no-cache",
	"Priority":                    "u=0, i",
	"Upgrade-Insecure-Requests":   "1",
	"Sec-Ch-Ua":                   `"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"`,
	"Sec-Ch-Ua-Arch":              `"x86"`,
	"Sec-Ch-Ua-Bitness":           `"64"`,
	"Sec-Ch-Ua-Form-Factors":      `"Desktop"`,
	"Sec-Ch-Ua-Full-Version":      `"146.0.7680.154"`,
	"Sec-Ch-Ua-Full-Version-List": `"Chromium";v="146.0.7680.154", "Not-A.Brand";v="24.0.0.0", "Google Chrome";v="146.0.7680.154"`,
	"Sec-Ch-Ua-Mobile":            "?0",
	"Sec-Ch-Ua-Model":             `""`,
	"Sec-Ch-Ua-Platform":          `"Windows"`,
	"Sec-Ch-Ua-Platform-Version":  `"19.0.0"`,
	"Sec-Ch-Ua-Wow64":             "?0",
	"Sec-Fetch-Dest":              "document",
	"Sec-Fetch-Mode":              "navigate",
	"Sec-Fetch-Site":              "none",
	"Sec-Fetch-User":              "?1",
}

var geminiHeaders = map[string]string{
	"Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
	"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6",
}

var geminiWhitelist = func() map[string]bool {
	codes := `ALB DZA ASM AND AGO AIA ATA ATG ARG ARM ABW AUS AUT AZE BHS BHR BGD BRB BEL BLZ BEN BMU BTN BOL BIH BWA BRA
IOT VGB BRN BGR BFA BDI CPV KHM CMR CAN BES CYM CAF TCD CHL CXR CCK COL COM COK CRI CIV HRV CUW CZE COD DNK DJI
DMA DOM ECU EGY SLV GNQ ERI EST SWZ ETH FLK FRO FJI FIN FRA GUF PYF ATF GAB GMB GEO DEU GHA GIB GRC GRL GRD GLP
GUM GTM GGY GIN GNB GUY HTI HKG HMD HND HUN ISL IND IDN IRQ IRL IMN ISR ITA JAM JPN JEY JOR KAZ KEN KIR XKX KWT KGZ
LAO LVA LBN LSO LBR LBY LIE LTU LUX MDG MWI MYS MDV MLI MLT MHL MTQ MRT MUS MYT MEX FSM MDA MCO MNG MNE MSR MAR MAC
MOZ MMR NAM NRU NPL NLD NCL NZL NIC NER NGA NIU NFK MKD MNP NOR OMN PAK PLW PSE PAN PNG PRY PER PHL PCN POL PRT
PRI QAT CYP COG REU ROU RWA BLM SHN KNA LCA MAF SPM VCT WSM SMR STP SAU SEN SRB SYC SLE SGP SXM SVK SVN SLB SOM
ZAF SGS KOR SSD ESP LKA SDN SUR SJM SWE CHE TWN TJK TZA THA TLS TGO TKL TON TTO TUN TUR TKM TCA TUV UGA UKR ARE
GBR USA UMI URY VIR UZB VUT VAT VEN VNM WLF ESH YEM ZMB ZWE ALA`
	m := map[string]bool{}
	for _, c := range strings.Fields(codes) {
		m[c] = true
	}
	return m
}()

func fetchText(cfg *Config, url, ua, cookie string, headers map[string]string, timeout time.Duration) string {
	req, err := http.NewRequest(http.MethodGet, url, nil)
	if err != nil {
		return ""
	}
	if ua != "" {
		req.Header.Set("User-Agent", ua)
	}
	if cookie != "" {
		req.Header.Set("Cookie", cookie)
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := HTTPClient(cfg, timeout).Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	return string(b)
}

func probeYouTubePremium(cfg *Config) MediaResult {
	body := fetchText(cfg, "https://www.youtube.com/premium", ytUA, ytCookie, ytHeaders, mediaTimeout)
	if body == "" {
		return MediaResult{Status: StatusNA}
	}
	region := ""
	if m := ytGLRe.FindStringSubmatch(body); m != nil {
		region = m[1]
	}
	if strings.Contains(body, "www.google.cn") {
		return MediaResult{Status: StatusCN, Region: "CN"}
	}
	if strings.Contains(body, `"content":"YouTube Premium 在你所在的国家/地区尚未推出"`) ||
		strings.Contains(body, `"content":"尽情享受所有 YouTube 内容，不受任何广告干扰。"`) ||
		strings.Contains(body, `"content":"无法享受此优惠"`) {
		return MediaResult{Status: StatusUnlocked, Region: region}
	}
	return MediaResult{Status: StatusUnknown, Region: region}
}

func probeGooglePlay(cfg *Config) MediaResult {
	body := fetchText(cfg, "https://play.google.com/store/games", playUA, "", playHeaders, mediaTimeout)
	if body == "" {
		return MediaResult{Status: StatusNA}
	}
	m := playRegionRe.FindStringSubmatch(body)
	if m == nil {
		return MediaResult{Status: StatusUnknown}
	}
	if m[1] == "CN" {
		return MediaResult{Status: StatusFailed, Region: "CN"}
	}
	return MediaResult{Status: StatusUnlocked, Region: m[1]}
}

func probeGemini(cfg *Config) MediaResult {
	body := fetchText(cfg, "https://gemini.google.com", ytUA, geminiCookie, geminiHeaders, mediaTimeout)
	if body == "" {
		return MediaResult{Status: StatusNA}
	}
	m := geminiRegionRe.FindStringSubmatch(body)
	if m == nil {
		return MediaResult{Status: StatusUnknown}
	}
	if geminiWhitelist[m[1]] {
		return MediaResult{Status: StatusUnlocked, Region: m[1]}
	}
	return MediaResult{Status: StatusFailed, Region: m[1]}
}

func fetchEgressIP(cfg *Config) string {
	for _, u := range []string{"https://api.ip.sb/ip", "https://ifconfig.me/ip", "https://api.ipify.org"} {
		body := strings.TrimSpace(fetchText(cfg, u, "curl/8.0", "", nil, 8*time.Second))
		if body != "" && ipLikeRe.MatchString(body) {
			return strings.Trim(body, "[]")
		}
	}
	return ""
}

func fetchGeo(cfg *Config, ip string) (country, cc, org string) {
	raw := fetchText(cfg, "http://ip-api.com/json/"+ip+"?fields=status,country,countryCode,org,isp,as", "", "", nil, 12*time.Second)
	var a struct {
		Status, Country, CountryCode, Org, ISP string
	}
	if json.Unmarshal([]byte(raw), &a) == nil && a.Status == "success" {
		return a.Country, strings.ToUpper(a.CountryCode), firstNonEmpty(a.Org, a.ISP)
	}
	raw = fetchText(cfg, "https://ipapi.co/"+ip+"/json/", "", "", nil, 12*time.Second)
	var b struct {
		CountryName string `json:"country_name"`
		CountryCode string `json:"country_code"`
		Org         string `json:"org"`
	}
	if json.Unmarshal([]byte(raw), &b) == nil && b.CountryName != "" {
		return b.CountryName, strings.ToUpper(b.CountryCode), b.Org
	}
	return "", "", ""
}

func checkPort25(cfg *Config) string {
	d := net.Dialer{Timeout: 5 * time.Second}
	if cfg.BindIP != "" {
		d.LocalAddr = &net.TCPAddr{IP: net.ParseIP(strings.Trim(cfg.BindIP, "[]"))}
	}
	c, err := d.Dial("tcp", "gmail-smtp-in.l.google.com:25")
	if err != nil {
		if cfg.BindIP != "" && strings.Contains(cfg.BindIP, ":") {
			return "untested"
		}
		return "blocked"
	}
	_ = c.Close()
	return "open"
}

// ProbeUnlock runs the YouTube Premium / Google Play / Gemini unlock checks in parallel.
func ProbeUnlock(cfg *Config) UnlockResult {
	r := UnlockResult{Node: cfg.NodeName, Alias: firstNonEmpty(cfg.NodeAlias, cfg.NodeName)}
	r.IP = fetchEgressIP(cfg)
	if r.IP == "" {
		r.IP = cfg.PublicIP
	}
	if r.IP == "" {
		r.Error = "无法获取出口 IP"
		return r
	}
	var cc string
	r.Country, cc, r.Org = fetchGeo(cfg, r.IP)

	var wg sync.WaitGroup
	wg.Add(4)
	go func() { defer wg.Done(); r.YT = probeYouTubePremium(cfg) }()
	go func() { defer wg.Done(); r.Play = probeGooglePlay(cfg) }()
	go func() { defer wg.Done(); r.Gemini = probeGemini(cfg) }()
	go func() { defer wg.Done(); r.Port25 = checkPort25(cfg) }()
	wg.Wait()

	if r.YT.Region == "" && r.YT.Status != StatusNA {
		r.YT.Region = cc
	}
	if r.Play.Region == "" && r.Play.Status != StatusNA {
		r.Play.Region = cc
	}
	return r
}

// MediaIcon renders a status as "icon `REGION`" like the original report.
func MediaIcon(m MediaResult) string {
	reg := m.Region
	if reg == "" {
		reg = "--"
	}
	switch m.Status {
	case StatusCN:
		return "🟡 CN"
	case StatusNA:
		return "⚪ N/A"
	case StatusUnlocked:
		return "🟢 " + reg
	case StatusFailed:
		return "🔴 " + reg
	default:
		return "⚪ " + reg
	}
}

func port25Label(s string) string {
	switch s {
	case "open":
		return "✅ 畅通"
	case "blocked":
		return "❌ 封堵"
	default:
		return "⏸️ 未测"
	}
}

func formatUnlockReport(cfg *Config, r UnlockResult) string {
	alias := htmlEscape(r.Alias)
	if r.Error != "" {
		return fmt.Sprintf("❌ <b>IP 解锁检测失败</b>\n📍 节点：<code>%s</code>\n⚠️ %s", alias, htmlEscape(r.Error))
	}
	warn := ""
	if r.YT.Status == StatusCN {
		warn = "\n🚨 <b>YouTube 判定为送中（google.cn）。</b>"
	} else if r.Play.Status == StatusFailed && r.Play.Region == "CN" {
		warn = "\n🚨 <b>Google Play 判定为中国大陆。</b>"
	}
	now := time.Now().UTC().Format("2006-01-02 15:04:05 UTC")
	return fmt.Sprintf(`🎯 <b>IP-Sentinel IP 解锁报告</b>
📍 节点：<code>%s</code>
🌐 地址：<code>%s</code> %s%s

<b>🎬 核心业务解锁</b>
• <b>YouTube Premium:</b> <code>%s</code>
• <b>Google Play:</b> <code>%s</code>
• <b>Gemini:</b> <code>%s</code>

<b>✉️ 25 端口出站:</b> %s

⏱️ <code>%s</code> | ⚙️ <code>v%s</code>`,
		alias, htmlEscape(r.IP), htmlEscape(r.Country), warn,
		MediaIcon(r.YT), MediaIcon(r.Play), MediaIcon(r.Gemini),
		port25Label(r.Port25), now, htmlEscape(cfg.AgentVersion))
}
