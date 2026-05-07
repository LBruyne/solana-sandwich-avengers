package sol

import (
	"sync"
	"sandwich-detector/types"
)

type BlockCache struct {
	mu      sync.RWMutex
	size    int
	blocks  []*types.Block
	index   int
	leaders map[uint64]string // slot -> leader
}

func NewBlockCache(size int) *BlockCache {
	return &BlockCache{
		size:    size,
		blocks:  make([]*types.Block, 0, size),
		leaders: make(map[uint64]string, size),
	}
}

func (c *BlockCache) Put(block *types.Block) {
	if block == nil {
		return
	}
	c.mu.Lock()
	defer c.mu.Unlock()

	if len(c.blocks) < c.size {
		c.blocks = append(c.blocks, block)
	} else {
		old := c.blocks[c.index]
		if old != nil {
			delete(c.leaders, old.Slot)
		}
		c.blocks[c.index] = block
		c.index = (c.index + 1) % c.size
	}
}

func (c *BlockCache) Get(slot uint64) *types.Block {
	c.mu.RLock()
	defer c.mu.RUnlock()
	for _, b := range c.blocks {
		if b != nil && b.Slot == slot {
			return b
		}
	}
	return nil
}

func (c *BlockCache) AllBlocks() []*types.Block {
	c.mu.RLock()
	defer c.mu.RUnlock()
	copied := make([]*types.Block, len(c.blocks))
	copy(copied, c.blocks)
	return copied
}

func (c *BlockCache) Clear() {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.blocks = make([]*types.Block, 0, c.size)
	c.leaders = make(map[uint64]string, c.size)
	c.index = 0
}

func (c *BlockCache) Leader(slot uint64) (string, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	l, ok := c.leaders[slot]
	return l, ok
}

func (c *BlockCache) SetLeader(slot uint64, leader string) {
	c.mu.Lock()
	defer c.mu.Unlock()
	if leader == "" {
		delete(c.leaders, slot)
		return
	}
	c.leaders[slot] = leader
}

func (c *BlockCache) GetSlotLeader(slot uint64) (string, error) {
	if l, ok := c.Leader(slot); ok {
		return l, nil
	}
	l, err := getSlotLeaderFromDB(slot)
	if err != nil {
		return "", err
	}
	c.SetLeader(slot, l)
	return l, nil
}
