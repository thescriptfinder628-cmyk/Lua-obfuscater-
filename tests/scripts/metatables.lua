local Vector = {}
Vector.__index = Vector

function Vector.new(x, y)
  return setmetatable({ x = x, y = y }, Vector)
end

function Vector:add(other)
  return Vector.new(self.x + other.x, self.y + other.y)
end

function Vector:__tostring()
  return string.format("(%g, %g)", self.x, self.y)
end

Vector.__add = function(a, b) return a:add(b) end
Vector.__eq = function(a, b) return a.x == b.x and a.y == b.y end

local a = Vector.new(1, 2)
local b = Vector.new(3, 4)
local c = a + b
print(tostring(c), c.x, c.y)
print(a == Vector.new(1, 2), a == b)

local proxy = setmetatable({}, {
  __index = function(_, key) return "missing:" .. key end,
  __call = function(_, arg) return "called:" .. tostring(arg) end,
})
print(proxy.whatever, proxy("hi"))
