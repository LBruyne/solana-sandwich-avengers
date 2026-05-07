package main

import (
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

	if err := viper.MergeInConfig(); err != nil {
		logger.GlobalLogger.Error("Error reading config.yaml file, if you don't have config.yaml file, please create one from config.example.yaml", "err", err)
	}

	if err := godotenv.Load(config.ConfigPath + ".env"); err != nil {
		logger.GlobalLogger.Error("Error reading .env file, if you don't have .env file, please create one from .env.example", "err", err)
	}

	viper.AutomaticEnv()
}

func initDB() {
	ch := db.NewClickhouse()
	defer ch.Close()

	logger.GlobalLogger.Info("Try to ensure database and tables exist")

	if err := ch.EnsureDatabaseExists(); err != nil {
		logger.GlobalLogger.Error("Failed to ensure database", "err", err)
		return
	}

	if err := ch.CreateTables(); err != nil {
		logger.GlobalLogger.Error("Failed to create tables", "err", err)
	}
}

func main() {
	initConfig()
	initDB()
	if err := cmd.RootCmd.Execute(); err != nil {
		logger.GlobalLogger.Error("Error executing command", "err", err)
	}

	logger.CloseAll()
}
