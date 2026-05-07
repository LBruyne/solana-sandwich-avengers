package utils

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"
	"sandwich-detector/config"
)

func GetUrlResponse(url string, params map[string]string, result any, logger *slog.Logger) error {
	return GetUrlResponseWithRetry(url, params, result, 1, logger)
}

func GetUrlResponseWithRetry(url string, params map[string]string, result any, retry int, logger *slog.Logger) error {
	reqUrl := url
	if len(params) > 0 {
		q := "?"
		for k, v := range params {
			q += fmt.Sprintf("%s=%s&", k, v)
		}
		reqUrl += q[:len(q)-1] // Remove trailing '&'
	}

	var lastErr error
	for i := 0; i < retry; i++ {
		lastErr = doGet(reqUrl, result)
		if lastErr == nil {
			return nil
		}
		logger.Warn("GET request failed, retrying...", "url", reqUrl, "attempt", i+1, "err", lastErr)
		time.Sleep(config.DefaultRetryInterval)
	}
	return fmt.Errorf("GET request failed after %d attempts: %w", retry, lastErr)
}

func PostUrlResponse(url string, body any, result any, logger *slog.Logger) error {
	return PostUrlResponseWithRetry(url, body, result, 1, logger)
}

func PostUrlResponseWithRetry(url string, body any, result any, retry int, logger *slog.Logger) error {
	var lastErr error
	for i := 0; i < retry; i++ {
		lastErr = doPost(url, body, result)
		if lastErr == nil {
			return nil
		}
		logger.Warn("POST request failed, retrying...", "url", url, "body", body, "attempt", i+1, "err", lastErr)
		time.Sleep(config.DefaultRetryInterval)
	}
	return fmt.Errorf("POST request failed after %d attempts: %w", retry, lastErr)
}

func doGet(url string, result any) error {
	ctx, cancel := context.WithTimeout(context.Background(), config.DefaultTimeout)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("failed to create GET request: %w", err)
	}

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("GET request error: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		bodyBytes, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("GET request returned status %d: %s", resp.StatusCode, string(bodyBytes))
	}

	dec := json.NewDecoder(resp.Body)
	if err := dec.Decode(result); err != nil {
		return fmt.Errorf("failed to stream and unmarshal GET response: %w", err)
	}

	return nil
}

func doPost(url string, body any, result any) error {
	ctx, cancel := context.WithTimeout(context.Background(), config.DefaultTimeout)
	defer cancel()

	bodyBytes, err := json.Marshal(body)
	if err != nil {
		return fmt.Errorf("failed to marshal POST body: %w", err)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(bodyBytes))
	if err != nil {
		return fmt.Errorf("failed to create POST request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("POST request error: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		bodyResp, _ := io.ReadAll(resp.Body)
		return fmt.Errorf("POST request returned status %d: %s", resp.StatusCode, string(bodyResp))
	}

	dec := json.NewDecoder(resp.Body)
	if err := dec.Decode(result); err != nil {
		return fmt.Errorf("failed to stream and unmarshal POST response: %w", err)
	}

	return nil
}
