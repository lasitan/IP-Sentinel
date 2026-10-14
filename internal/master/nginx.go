package master

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

const nginxSiteName = "ip-sentinel"

// DeployNginx writes a reverse-proxy site for Master (TG webhook + /ws) and reloads nginx.
// enableTLS=false：仅 80 反代（含 WebSocket），不写 443、不跑 certbot。
// enableTLS=true：先落 80 反代并通过 nginx -t，再 certbot，成功后切换 443。
func DeployNginx(domain, upstream, email string, enableTLS bool) (string, error) {
	domain = strings.TrimSpace(domain)
	if domain == "" {
		return "", fmt.Errorf("域名不能为空")
	}
	if upstream == "" {
		upstream = "http://127.0.0.1:19530"
	}

	confDir := "/etc/nginx/sites-available"
	enabledDir := "/etc/nginx/sites-enabled"
	if _, err := os.Stat(confDir); err != nil {
		confDir = "/etc/nginx/conf.d"
		enabledDir = ""
	}
	_ = os.MkdirAll(confDir, 0o755)

	httpACME := fmt.Sprintf(`
server {
    listen 80;
    listen [::]:80;
    server_name %s;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
`, domain)

	httpsBlock := fmt.Sprintf(`
server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name %s;

    ssl_certificate     /etc/letsencrypt/live/%s/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/%s/privkey.pem;

    location / {
        proxy_pass %s;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
`, domain, domain, domain, upstream)

	// 仅 80：反代全部路径（含 /tg、/ws），支持 WebSocket Upgrade
	plainBlock := fmt.Sprintf(`
server {
    listen 80;
    listen [::]:80;
    server_name %s;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }

    location / {
        proxy_pass %s;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
`, domain, upstream)

	confPath := filepath.Join(confDir, nginxSiteName+".conf")
	// 无论是否 TLS，先写可加载的 80 反代，避免无证书时 nginx -t 失败
	if err := os.WriteFile(confPath, []byte(plainBlock), 0o644); err != nil {
		return "", fmt.Errorf("写入 nginx 配置失败: %w", err)
	}

	if enabledDir != "" {
		_ = os.MkdirAll(enabledDir, 0o755)
		link := filepath.Join(enabledDir, nginxSiteName+".conf")
		_ = os.Remove(link)
		if err := os.Symlink(confPath, link); err != nil {
			_ = os.WriteFile(link, []byte(plainBlock), 0o644)
		}
	}

	if out, err := exec.Command("nginx", "-t").CombinedOutput(); err != nil {
		return "", fmt.Errorf("nginx -t 失败: %s", strings.TrimSpace(string(out)))
	}
	if out, err := exec.Command("systemctl", "reload", "nginx").CombinedOutput(); err != nil {
		if out2, err2 := exec.Command("nginx", "-s", "reload").CombinedOutput(); err2 != nil {
			return "", fmt.Errorf("reload nginx 失败: %s / %s", strings.TrimSpace(string(out)), strings.TrimSpace(string(out2)))
		}
	}

	msg := fmt.Sprintf("✅ Nginx 反代已部署（80 + WebSocket）\n域名: `%s`\n上游: `%s`\n配置: `%s`", domain, upstream, confPath)
	if !enableTLS {
		msg += "\n📡 本机仅监听 80（无本地证书）。若前面有 Cloudflare/CDN，公网仍可用 `https://` / `wss://`。"
		return msg, nil
	}

	args := []string{"certonly", "--nginx", "-d", domain, "--non-interactive", "--agree-tos"}
	if email != "" {
		args = append(args, "-m", email)
	} else {
		args = append(args, "--register-unsafely-without-email")
	}
	if out, err := exec.Command("certbot", args...).CombinedOutput(); err != nil {
		msg += fmt.Sprintf("\n⚠️ certbot 失败（已保持 80 反代）: %s", strings.TrimSpace(string(out)))
		return msg, nil
	}

	tlsConf := httpACME + "\n" + httpsBlock
	if err := os.WriteFile(confPath, []byte(tlsConf), 0o644); err != nil {
		msg += "\n⚠️ 证书已签发但写入 443 配置失败，仍使用 80 反代"
		return msg, nil
	}
	if enabledDir != "" {
		link := filepath.Join(enabledDir, nginxSiteName+".conf")
		_ = os.WriteFile(link, []byte(tlsConf), 0o644)
	}
	if out, err := exec.Command("nginx", "-t").CombinedOutput(); err != nil {
		// 回滚到 80，避免 nginx 起不来
		_ = os.WriteFile(confPath, []byte(plainBlock), 0o644)
		_ = exec.Command("nginx", "-t").Run()
		_ = exec.Command("systemctl", "reload", "nginx").Run()
		msg += fmt.Sprintf("\n⚠️ 443 配置校验失败，已回滚 80: %s", strings.TrimSpace(string(out)))
		return msg, nil
	}
	_ = exec.Command("systemctl", "reload", "nginx").Run()
	msg += "\n🔒 TLS 证书已签发，HTTPS 已启用"
	return msg, nil
}
