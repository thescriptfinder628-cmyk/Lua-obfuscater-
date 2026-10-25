local a = "tab\there"
local b = 'single \'quote\''
local c = "both \"double\" and 'single'"
local d = [[long
string]]
local e = [==[with ]] inside]==]
local f = "\65\66\67"
local g = "\x41\x42"
local h = "back\\slash"
print(a)
print(b)
print(c)
print(d)
print(e)
print(f, g, h)
print(("abc"):byte(1, -1))
print(string.format("%5.2f|%s|%q", 3.14159, "hi", "a\nb"))
print(#d, d:find("string"), ("x"):rep(3))
local concat = "a" .. 1 .. "b" .. 2.5
print(concat)
