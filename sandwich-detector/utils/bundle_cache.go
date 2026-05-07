package utils

// A simple LRU cache for bundle IDs
type BundleCache struct {
	set      map[string]struct{}
	order    []string
	capacity int
}

const DefaultBundleCacheCapacity = 100000

func NewBundleCache() *BundleCache {
	return &BundleCache{
		set:      make(map[string]struct{}),
		capacity: DefaultBundleCacheCapacity,
		order:    make([]string, 0, DefaultBundleCacheCapacity),
	}
}

func (c *BundleCache) Has(bundleId string) bool {
	_, exists := c.set[bundleId]
	return exists
}

func (c *BundleCache) Add(bundleId string) {
	if c.Has(bundleId) {
		return
	}
	if len(c.order) >= c.capacity {
		old := c.order[0]
		c.order = c.order[1:]
		delete(c.set, old)
	}
	c.set[bundleId] = struct{}{}
	c.order = append(c.order, bundleId)
}

func (c *BundleCache) Len() int {
	return len(c.set)
}
