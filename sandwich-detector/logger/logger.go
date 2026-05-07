package logger

import (
	"fmt"
	"io"
	"log"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"
	"sandwich-detector/config"
)

const MaxLogSize = 50 * 1024 * 1024 // 100 MB

var (
	SolLogger, JitoLogger, GlobalLogger *slog.Logger
	consoleEnabled                      = true

	globalRW, solRW, jitoRW *rotatingWriter
)

// Thread-safe writer that rotates files when they exceed max size.
type rotatingWriter struct {
	mu      sync.Mutex
	file    *os.File
	dir     string
	prefix  string // e.g. "detector_20250925_101122_global"
	ext     string // ".log"
	size    int64
	maxSize int64
	timefmt string
}

func newRotatingWriter(dir, prefix string, maxSize int64) (*rotatingWriter, error) {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return nil, err
	}
	rw := &rotatingWriter{
		dir:     dir,
		prefix:  prefix,
		ext:     ".log",
		maxSize: maxSize,
		timefmt: "20060102150405",
	}
	if err := rw.rotateNew(); err != nil {
		return nil, err
	}
	return rw, nil
}

func (w *rotatingWriter) currentName() string {
	return filepath.Join(w.dir, w.prefix+w.ext)
}

func (w *rotatingWriter) rotateNew() error {
	if w.file != nil {
		_ = w.file.Close()
	}

	f, err := os.OpenFile(w.currentName(), os.O_CREATE|os.O_TRUNC|os.O_RDWR, 0o666)
	if err != nil {
		return err
	}
	w.file = f
	w.size = 0
	return nil
}

func (w *rotatingWriter) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()

	if w.size+int64(len(p)) > w.maxSize {
		if err := w.rotateNew(); err != nil {
			return 0, err
		}
	}
	n, err := w.file.Write(p)
	w.size += int64(n)
	return n, err
}

func (w *rotatingWriter) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.file != nil {
		return w.file.Close()
	}
	return nil
}

func SetConsoleEnabled(enabled bool) {
	consoleEnabled = enabled
	resetLoggers()
}

func InitLogs(cmdName string) {
	ensureLogDir()

	ts := time.Now().Format("20060102150405")

	var err error
	solRW, err = newRotatingWriter(config.LogPath, fmt.Sprintf("detector_%s_%s_sol", ts, cmdName), MaxLogSize)
	if err != nil {
		log.Fatal(err)
	}
	jitoRW, err = newRotatingWriter(config.LogPath, fmt.Sprintf("detector_%s_%s_jito", ts, cmdName), MaxLogSize)
	if err != nil {
		log.Fatal(err)
	}

	SolLogger = slog.New(newHandler(solRW))
	JitoLogger = slog.New(newHandler(jitoRW))
	resetLoggers()
}

func init() {
	ensureLogDir()
	ts := time.Now().Format("20060102150405")

	var err error
	globalRW, err = newRotatingWriter(config.LogPath, fmt.Sprintf("detector_%s_global", ts), MaxLogSize)
	if err != nil {
		log.Fatal(err)
	}
	GlobalLogger = slog.New(newHandler(globalRW))
	resetLoggers()
}

func CloseAll() {
	if globalRW != nil {
		_ = globalRW.Close()
	}
	if solRW != nil {
		_ = solRW.Close()
	}
	if jitoRW != nil {
		_ = jitoRW.Close()
	}
}

func ensureLogDir() {
	if err := os.MkdirAll(config.LogPath, 0o755); err != nil {
		log.Fatal(err)
	}
}

func newHandler(fileWriter io.Writer) slog.Handler {
	w := fileWriter
	if consoleEnabled {
		w = io.MultiWriter(os.Stdout, fileWriter)
	}
	return slog.NewTextHandler(w, &slog.HandlerOptions{
		AddSource: true,
	})
}

func resetLoggers() {
	if GlobalLogger != nil && globalRW != nil {
		GlobalLogger = slog.New(newHandler(globalRW))
	}
	if SolLogger != nil && solRW != nil {
		SolLogger = slog.New(newHandler(solRW))
	}
	if JitoLogger != nil && jitoRW != nil {
		JitoLogger = slog.New(newHandler(jitoRW))
	}
}
