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

	httpBlock := fmt.Sprintf(`
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

	plainBlock := fmt.Sprintf(`
server {
    listen 80;
    listen [::]:80;
    server_name %s;

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
	content := plainBlock
	if enableTLS {
		content = httpBlock + "\n" + httpsBlock
	}
	if err := os.WriteFile(confPath, []byte(content), 0o644); err != nil {
		return "", fmt.Errorf("写入 nginx 配置失败: %w", err)
	}

	if enabledDir != "" {
		_ = os.MkdirAll(enabledDir, 0o755)
		link := filepath.Join(enabledDir, nginxSiteName+".conf")
		_ = os.Remove(link)
		if err := os.Symlink(confPath, link); err != nil {
			// copy fallback
			_ = os.WriteFile(link, []byte(content), 0o644)
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

	msg := fmt.Sprintf("✅ Nginx 反代已部署\n域名: `%s`\n上游: `%s`\n配置: `%s`", domain, upstream, confPath)
	if enableTLS {
		args := []string{"certonly", "--nginx", "-d", domain, "--non-interactive", "--agree-tos"}
		if email != "" {
			args = append(args, "-m", email)
		} else {
			args = append(args, "--register-unsafely-without-email")
		}
		if out, err := exec.Command("certbot", args...).CombinedOutput(); err != nil {
			msg += fmt.Sprintf("\n⚠️ certbot 失败（HTTP 反代仍可用）: %s", strings.TrimSpace(string(out)))
		} else {
			// rewrite with TLS block after cert exists
			_ = os.WriteFile(confPath, []byte(httpBlock+"\n"+httpsBlock), 0o644)
			_ = exec.Command("nginx", "-t").Run()
			_ = exec.Command("systemctl", "reload", "nginx").Run()
			msg += "\n🔒 TLS 证书已签发，HTTPS 已启用"
		}
	}
	return msg, nil
}
