package sol

import (
	"container/list"
	"sync"
)

// AMMPoolLRU is a thread-safe LRU set for caching known AMM pool addresses.
// It provides O(1) Add and Contains operations with bounded memory usage.
// When memecoin pools are numerous and ephemeral, this prevents unbounded
// memory growth while keeping frequently queried pools warm.
type AMMPoolLRU struct {
	mu       sync.Mutex
	capacity int
	items    map[string]*list.Element
	order    *list.List // front = most recently used
}

// NewAMMPoolLRU creates a new AMMPoolLRU with the given capacity.
func NewAMMPoolLRU(capacity int) *AMMPoolLRU {
	return &AMMPoolLRU{
		capacity: capacity,
		items:    make(map[string]*list.Element, capacity),
		order:    list.New(),
	}
}

// Add inserts a pool address into the cache. If it already exists, it is promoted
// to the front (most recently used). If the cache is full, the least-recently-used
// entry is evicted.
func (c *AMMPoolLRU) Add(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if elem, ok := c.items[key]; ok {
		c.order.MoveToFront(elem)
		return
	}
	if c.order.Len() >= c.capacity {
		oldest := c.order.Back()
		if oldest != nil {
			c.order.Remove(oldest)
			delete(c.items, oldest.Value.(string))
		}
	}
	elem := c.order.PushFront(key)
	c.items[key] = elem
}

// Remove deletes a pool address from the cache if it exists.
func (c *AMMPoolLRU) Remove(key string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if elem, ok := c.items[key]; ok {
		c.order.Remove(elem)
		delete(c.items, key)
	}
}

// Contains checks whether a pool address is in the cache. If found, the entry
// is promoted to the front (most recently used) to keep frequently queried pools warm.
func (c *AMMPoolLRU) Contains(key string) bool {
	c.mu.Lock()
	defer c.mu.Unlock()
	if elem, ok := c.items[key]; ok {
		c.order.MoveToFront(elem)
		return true
	}
	return false
}

// ─────────────────────────────────────────────────────────────────────────────
// AccountOwnerLRU — key→value LRU cache for account owner lookups
// ─────────────────────────────────────────────────────────────────────────────

type ownerEntry struct {
	key   string
	value string
}

// AccountOwnerLRU is a thread-safe LRU map caching account address → owner program.
type AccountOwnerLRU struct {
	mu       sync.Mutex
	capacity int
	items    map[string]*list.Element
	order    *list.List // front = most recently used
}

func NewAccountOwnerLRU(capacity int) *AccountOwnerLRU {
	return &AccountOwnerLRU{
		capacity: capacity,
		items:    make(map[string]*list.Element, capacity),
		order:    list.New(),
	}
}

// Put stores an address→owner mapping. Promotes if already present.
func (c *AccountOwnerLRU) Put(key, value string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if elem, ok := c.items[key]; ok {
		elem.Value = ownerEntry{key: key, value: value}
		c.order.MoveToFront(elem)
		return
	}
	if c.order.Len() >= c.capacity {
		oldest := c.order.Back()
		if oldest != nil {
			c.order.Remove(oldest)
			delete(c.items, oldest.Value.(ownerEntry).key)
		}
	}
	elem := c.order.PushFront(ownerEntry{key: key, value: value})
	c.items[key] = elem
}

// Get returns (owner, true) if found, or ("", false) if not cached.
func (c *AccountOwnerLRU) Get(key string) (string, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if elem, ok := c.items[key]; ok {
		c.order.MoveToFront(elem)
		return elem.Value.(ownerEntry).value, true
	}
	return "", false
}
