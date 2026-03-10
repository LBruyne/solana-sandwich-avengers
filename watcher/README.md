# Solana MEV watcher

`Watcher` provides several commands to monitor and analyze Sandwich activities on the Solana blockchain.

## Build database

We use [ClickHouse](https://clickhouse.com/) as the default database.

In Ubuntu, you can install it by following the following commands, also refer to [ClickHouse official installation guide](https://clickhouse.com/docs/install/debian_ubuntu):

```bash
# Install prerequisite packages
sudo apt-get install -y apt-transport-https ca-certificates curl gnupg

# Download the ClickHouse GPG key and store it in the keyring
curl -fsSL 'https://packages.clickhouse.com/rpm/lts/repodata/repomd.xml.key' | sudo gpg --dearmor -o /usr/share/keyrings/clickhouse-keyring.gpg

# Get the system architecture
ARCH=$(dpkg --print-architecture)

# Add the ClickHouse repository to apt sources
echo "deb [signed-by=/usr/share/keyrings/clickhouse-keyring.gpg arch=${ARCH}] https://packages.clickhouse.com/deb stable main" | sudo tee /etc/apt/sources.list.d/clickhouse.list

# Update apt package lists
sudo apt-get update

sudo apt-get install -y clickhouse-server clickhouse-client
# Set and remember your password for defualt `user`

sudo service clickhouse-server start

clickhouse-client
```

You will have a default user `default` with no password (in some cases you need to reset the password if you cannot login to Clickhouse). You can create a new user (with password) and database for the Watcher.

Try to create a database `solwich`:

```sql
CREATE DATABASE solwich;
exit
```

## Setup environment and configs

Now, create your own configuration files and environment variables about database and RPCs in `.env` and `config.yaml` under the `watcher` folder.

- Refer to [.env.example](./.env.example) and create your own `.env` file. You need to set the database connection parameters.
- Refer to [config.example.yaml](./config.example.yaml) and create your own `config.yaml` file. You need to set the Solana RPC URL and Jito API URL.
- Update the `LogPath` in [config.go](./config/config.go) to your desired log folder. Make sure the folder path is writable.

## Setup Golang environment and build the project

We use a `go 1.24` version. Install it from [golang offical website](https://go.dev/dl/). The below commands are for x86_64 (AMD) Ubuntu machine (use `uname -m` to see your arch):

```bash
wget https://go.dev/dl/go1.24.7.linux-amd64.tar.gz
sudo tar -C /usr/local -xzf go1.24.7.linux-amd64.tar.gz
# Append the following lines to your ~/.bashrc (or ~/.zshrc if you use Zsh):
echo "export PATH=\$PATH:/usr/local/go/bin" >> ~/.bashrc
echo "export GOROOT=/usr/local/go" >> ~/.bashrc
echo "export GOPATH=\$HOME/go" >> ~/.bashrc
echo "export PATH=\$PATH:\$GOPATH/bin" >> ~/.bashrc
source ~/.bashrc

# Verify the installation
go version # should show go1.24.7
```

Make sure your `GOPATH` and `GOROOT` are set correctly.

Finally, build the project under the `watcher` folder:

```bash
go mod tidy
go build .
```

## Commands

You can refer to the `./scripts` folder for example scripts to run the commands below.

### Fetch Jito bundles

`jito` command fetches Jito bundles from the Jito API and stores them into the database.
Currently, it fetches bundles by slot, and checkes detected sandwiches in each bundle automatically.

We also provide a `jito_recent` file to fetch recent Jito bundles in a loop.

```bash
go run main.go jito -s <start_slot>
```

### Fetch Solana slot leader

`leader` command fetches Solana slot leaders from the Solana RPC and stores them into the database. It use the official Solana RPC `getSlotLeader` API to fetch each slot's leader.

You can specify the start slot. If not specified, it will fetch from the latest slot to the current slot.

```bash
go run main.go leader -s <start_slot>
```

### Fetch Solana transactions and find Sandwiches (under development)

`sandwich` command fetches Solana transactions (in each block) from the Solana RPC.
Then it analyzes the transactions to find Sandwich activities and stores them into the database.

```bash
go run main.go sandwich -s <start_slot>
```

We focus on both InBlock Sandwich and CrossBlock Sandwich.

The fetch process is done slot by slot, in parallel with multiple workers.
The sandwich analysis is done also in parallel with multiple workers.

You can specify the start slot. If not specified, it will fetch from the latest slot to the current slot.
