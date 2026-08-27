package main

import (
	"os"

	"github.com/joho/godotenv"
	"github.com/spf13/viper"

	"sandwich-detector/cmd"
	"sandwich-detector/config"
	"sandwich-detector/db"
	"sandwich-detector/logger"
)

func initConfig() {
	viper.SetConfigName("config")
	viper.SetConfigType("yaml")
	viper.AddConfigPath(config.ConfigPath)

	// Fail fast: continuing with an empty config only turns a missing file into a confusing
	// RPC or ClickHouse error several layers down.
	if err := viper.MergeInConfig(); err != nil {
		logger.GlobalLogger.Error("Cannot read config.yaml -- copy config.example.yaml to config.yaml and fill in the endpoints", "err", err)
		os.Exit(1)
	}

	if err := godotenv.Load(config.ConfigPath + ".env"); err != nil {
		logger.GlobalLogger.Error("Cannot read .env -- copy .env.example to .env and fill in the credentials", "err", err)
		os.Exit(1)
	}

	viper.AutomaticEnv()
}

func initDB() {
	ch := db.NewClickhouse()
	defer ch.Close()

	logger.GlobalLogger.Info("Try to ensure database and tables exist")

	if err := ch.EnsureDatabaseExists(); err != nil {
		logger.GlobalLogger.Error("Failed to create the database -- check CLICKHOUSE_* in .env and that the server is reachable", "err", err)
		os.Exit(1)
	}

	if err := ch.CreateTables(); err != nil {
		logger.GlobalLogger.Error("Failed to create tables", "err", err)
		os.Exit(1)
	}
}

func main() {
	initConfig()
	cmd.Bootstrap = initDB
	if err := cmd.RootCmd.Execute(); err != nil {
		logger.GlobalLogger.Error("Error executing command", "err", err)
	}

	logger.CloseAll()
}
