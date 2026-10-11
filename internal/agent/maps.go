package agent

import (
	"context"
	"fmt"
	"time"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/emulation"
	"github.com/chromedp/chromedp"
)

// VisitMapsWithGeo opens Google Maps with geolocation override (chromedp).
func VisitMapsWithGeo(cfg *Config, lat, lng float64, mapsURL string) error {
	if !cfg.EnableMapsGeo {
		_, _, err := HTTPGet(cfg, mapsURL, 30*time.Second)
		return err
	}
	opts := append(chromedp.DefaultExecAllocatorOptions[:],
		chromedp.Flag("headless", true),
		chromedp.Flag("disable-blink-features", "AutomationControlled"),
		chromedp.Flag("no-first-run", true),
		chromedp.Flag("no-default-browser-check", true),
		chromedp.WindowSize(1920, 1080),
	)
	allocCtx, cancel := chromedp.NewExecAllocator(context.Background(), opts...)
	defer cancel()
	ctx, cancel2 := chromedp.NewContext(allocCtx)
	defer cancel2()
	ctx, cancel3 := context.WithTimeout(ctx, 90*time.Second)
	defer cancel3()

	script := `
(() => {
  const granted = { state: 'granted', onchange: null };
  if (navigator.permissions && navigator.permissions.query) {
    const orig = navigator.permissions.query.bind(navigator.permissions);
    navigator.permissions.query = (desc) =>
      desc && desc.name === 'geolocation' ? Promise.resolve(granted) : orig(desc);
  }
})();`

	acc := 10.0
	err := chromedp.Do(ctx,
		chromedp.Func(func(ctx context.Context, t *chromedp.Target) error {
			_, err := chromedp.Evaluate[any](script)(ctx, t)
			return err
		}),
		chromedp.Func(func(ctx context.Context, t *chromedp.Target) error {
			_, err := cdp.Call(ctx, t, emulation.SetGeolocationOverride, emulation.SetGeolocationOverrideParams{
				Latitude:  &lat,
				Longitude: &lng,
				Accuracy:  &acc,
			})
			return err
		}),
		chromedp.Navigate(mapsURL),
		chromedp.Sleep(5*time.Second),
	)
	if err != nil {
		cfg.Log("MAPS", "WARN", fmt.Sprintf("chromedp maps: %v；回退 HTTP", err))
		_, _, err2 := HTTPGet(cfg, mapsURL, 30*time.Second)
		return err2
	}
	return nil
}
