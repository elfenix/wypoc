
## Semantic Explanation of Syntax

### Literals

    literal_expr:
      | signed_number
      | strings
      | character
      | 'true'
      | 'false'
      | 'nil'

#### Signed Numbers

Integers leverage standard C semantics: -?\d+ or 0x\[a-fA-F0-9]+

    0xdead
    12345
    -10

Floating point numbers follow standard C semantics:

    3e1
    3.14159
    -18.24

#### Booleans

Specify true of false

    true # True
    false # False

#### Nil

`nil` is a keyword for a unset variable.

    nil

#### Symbol

A symbol is an identifier / name in Wyrm code. It may be specified
by a single quote preceding a valid wyrm name:

    'name

An implementation may limit significant characters in a symbol. An
implementation must support at least 31 characters of significance.

#### Strings

String literals are generally interned. Wyrm strings are considered
immutable.

Normal strings are simple double-quoted values:

    "this is a string"

Multiline strings may be specified using triple double quote:

    """This is a string
    this is still part of the string.""

Raw strings may be defined using C++ style literal:

    R"(Arbitrary Text)"
    R"extra(WE can noow have (" )extra"

#### Characters

A character literal is Clojure-style: a backslash followed by either a
single character, or one of a handful of named characters. It evaluates to
that character's numeric (u32) value - the same value string indexing
already produces, e.g. `\a == "asdf"[0]`.

    \a
    \newline
    \space
    \tab
    \return
    \backspace
    \formfeed
    \null

### Built-in Collections

#### Arrays

Arrays are defined as a set of variables of a single primitive type,
but that primitive type may be an 'object'.

Use brackets:

    [1, 2, 3, 4]

#### Pair / List

A pair is a mutable type that holds 2 objects. It may be defined by
quoted parens:

    '('a)

Quoted parens also allow specification of a list of pairs:

    '(1, 2, 3, 4, 5)

Note: the use of ',' operator to define elements. Define an improper
list utilize `'.` for the last element:

    '(1, 2, 3, 4, '. 6)

Pairs are internally provided to aid in the general representation
of SCHEME style primitives internally. 

#### Tuples

Tuples denoted by the comma operator (least precedence):

    1, 2, 3, 4

Single element tuple use parens to force the group:

    (1,)

Empty tuple may be specified by open-close parens '()'

#### Tables or Dictionaries

Dictionary definitions use a `'{` sigil rather than bare braces.

    '{ "Name": 15 }

Empty dictionary:

    '{}

### Operators

Numerical operators follow same rules as C/C++/Python:

    a ** 2     # Exponents
    a + b + c  # Addition, LTR
    a * b * c  # Multiplication, LTR
    a - b - c  # Subtraction, LTR
    a % b % c  # Modulus, LTR
    a / b / c  # Division, LTR

Bitwise operators:

    a & b
    a | b
    a ^ b

Boolean operators / set operators:

    a or b
    a and b
    not a
    a in b

Comparisons:

    a <= b
    a >= b
    a <=> b
    a < b
    b > b
    a == b
    a != b

Lookup Operator:

    arr[0]
    dictionary[key]

### Blocks

Wyrm follows Haskell's Layout-Rule. Preferred style is that of Python, braces
are intended to allow one-liners and compressed scripts. Statements may be
terminated by newline, semicolon, or a brace matching the block.

    # If statement
    if foo:
        action()
    another_action()

    # If statement, braces
    if foo { action(); } another_action();

### Variables

Variables defined by simply setting them:

    # Set with type
    foo: int = 5

    # Multivalue - type must be specified before
    foo, bar = 5, 4

A type hint may only be defined for a variable once within a scope.
A variable may be constrained to a type with a type hint statement:

    foo: int

A type hint constraint statement does _not_ define the variable - access
afterword is equivalent to an unset/undefined variables. Undefined
variables will generate an error if attempted to be evaluated:

    if foo:

Generates an error if not assigned. It's possible to test if a variable
is defined in the current scope utilizing the 'defined()' function. The
symbol name must be used:

    if defined('foo):
        foo = foo + 1
    else:
        foo = 1

Wyrm offers a 'set if unset' operator. Evaluation is short circuited
if the variable is already defined, otherwise the evaluation is processed:

    foo ?= 5
    foo_with_type: int ?= 5

A variable may have a type specified prior to assignment:

    foo: int
    if check:
        foo = 5
    else:
        foo = 10

Immutable variables may be created with the 'with' keyword. Setting a
variable defined using with is considered an error.

    with speed_of_light: float = 299_792_458.0;

Multiple constants can be defined using a with block:

    with:
        speed_of_light: float = 299_792_458.0;
        gravitational_constant = 6.6743e-11;

Static variables start with the '$' operator. A static variable is tied
to the symbol definition rather than the execution context. A static
variable should generally be initialized with the `?=` operator.

    fn call_count():
        $foo: int ?= 0
        $foo = $foo + 1
        return $foo

### Modules and Imports

Import is simple:

    import mod

Imports in subdirectories:

    import mod::baz::bar

Once a module is imported, the name scope operator can pull in:

    import mod
    mod::function()

From may be utilized to import names:

    from mod import function
    function()

The using keyword allows manipulation of names. Using on a bare module
will import all names defined by the module into the current namespace.

    import math
    using math

Using may also import individual symbols:

    using math::sin;

Or to create aliases:

    using sin = math::sin;

### Basic Functions

Basic functions should look exceedingly familiar to Python users. Most all
 the same rules apply – including no function overloading.

The basic syntax for a function is:

    fn [type...] name(parameters...) -> [result] block...

Most elements are optional, a minimal function definition with no parameters:

    fn hello():
        pass

Functions return a value with the return keyword statement:

    fn message():
        return "Hello World"

A function may routine multiple values:

    fn message() -> int, str, str:
        return 1984, "Text Here", "Text Here 2"

Return type may be specified:

    fn message() -> str:
        return "Hello World"

Parameters may be specified:

    fn message(name) -> str:
        return "Hello " + name

    fn message(greeting: str, name; str) -> str:
        return greeting + name
    
Variable length arguments may be collected by the '*' operator:

    fn message(*arguments) -> str:
        greeting, name = arguments
        return greeting + name

Arguments may have default values:

    fn message(name: str, greeting: str = "Hello") -> str:
        return greeting + name

The '/' special argument specifies prior arguments must be positional:

    fn message(name: str, /,  greeting: str = "Hello") -> str:
        return greeting + name

And '**' may be used to collect keyword argument into a dict:

    fn message(**kwargs) -> str:
        return kwargs["greeting"] + kwargs["name"]

### Control Flow

Basic control flow statements - if, while, and for.

    if condition:
        statements
    elif condition_2:
        statements
    elif condition_3:
        statements
    elif condition_n:
        statements
    else:
        statements

While statement

    while condition:
        statement
        if condition:
            continue
        if condition_2:
            break

For statement:

    for var in iterable:
        statement
        if condition:
            break
    else:
        statement_if_no_break

### Messages

A message is a method on a class. Messages may be dispatched utilizing the
message operator `!`.

In simple cases, a message may utilized just as a method call:

    arr_len = array_object!length();

A message may be executed on a tuple:

    (canvas, shape) ! draw();

### Basic Classes

Classes are used to define structure suitable for dynamic dispatch. Objects
work in a similar fashion to CLOS or Dylan. A class is a collection of variables
associated with an inheritance tree.

A basic class defines a data structure and inheritance tree. The class name
may then be used to specify dispatch of methods. A simple class is created
by simple defining the data structure:

    class person:
        slot name: str

    class family_member(person):
        slot relation: str

    class coordinate2d {
        slot x: float;
        slot y: float;
    }

Classes may have methods defined internally:

    class person:
        slot first_name: str = "John"
        slot last_name: str = "Doe"

        fn get_full_name() -> str:
            return last_name + " " + first_name

In the lexical scope of a class, internal memory defined by a
slot definition is accessible using the slot name directly. In
the above example 'last_name' and 'first_name' reference the
internal backing.

Note that the function 'get_full_name' has access to the internal
storage for first_name and last_name. A class method may also be
defined externally:

    fn [person] get_full_name() -> str:
        return this.last_name + " " + this.first_name

Class attributes and variables can be access with the `.` operator.
The `.` operator looks up the given name and creates an attribute
ref. Assigning to the attribute ref results in setting a property.

    person.first_name = "Sam"

Functions defined within the class scope will have attributes
within the created created class. These may be accessed and
called directly, though this is not normally recommended:

    person.get_full_name()

The preferred mechanism to call a method is to utilize the
message operator `!`. The message operator replaces the class
access operator:

    person!get_full_name()

The `!` operator creates a closure. The closure may be elided
in cases where the method is called directly, or it may
be stored:

    name_func = person!get_full_name
    name_func()

Methods may be defined external to the class. These may *not*
be called using attribute reference, but may be called via
the message operator.

    fn [person] get_name():
        return this.name

    fn [coordinate2d] length_squared() {
        return (x**2 + y**2)
    }

The `super` allows classes to call 'up' the inheritance tree.

    class coordinate3d(coordinate2d) {
        slot z: float;
    }

    fn [coordinate3d] length_squared:
        return super() + (z**2)

Objects are constructed with the 'new' keyword. The new keyword specifies
parameters to the constructor:

    person_instance = new person()

A class constructor may be specified within the class. The constructor defines
the default initialized variables for the object and then a block of code
that executes immediately after. All variables must have defined default

Basic syntax (within class) [FIXME/TODO]:

    init (arguments) with 'defaults block...' 'initialization block'

Example with init:

    class coordinate2d:
        slot x: float
        slot y: float
    
        init (x: float, y: float) with:
            x = x
            y = y

TODO TODO TODO: constructor syntax still ugly / needs improvement ????

#### Slots

The slot keyword defines a data element with the class. Definition of a slot
creates automatic getter/setter functions. The default value for a slot may
be specified:

    class person:
        slot name: str = "John Doe"

The with keyword allows specifying extended options on a slot. The
normal syntax of the with statement applies. Constants set within
the with statement are treated as parameters to the slot creator:

    class person
        slot name: str = "John Doe" with:
            setter = fn (value) { this.name = value; }
            getter = undefined;

The slot creator accepts the following parameters:

    setter: function with parameter for the new value

    getter: function that returns the value

### Coroutines

Coroutines generally follow the exact same syntax of functions. A basic
coroutine assume any/any for input and output.

    co simple():
        yield 1

Output may be specified:

    co count_to_5() -> int:
        yield 1
        yield 2
        yield 3
        yield 4
        yield 5

Coroutines may also accept values:

    co mirror_5x() -> float:
        a = yield 0
        a = yield a
        a = yield a
        a = yield a
        a = yield a
        yield a

The input type may be specified:

    co div2_1x() <- int -> float:
        a = yield 0
        yield a / 2.0


## Types and Type System

### Primitive Types

A wyrm variable reserves place for a primitive. A primitive tags the variable
type and holds a primitive value. The 'is' boolean operator checks that the
variable type matches and the primitive value is identical.

Primitive types:
  - boolean
  - float
  - int
  - object pointer

## Core Features

### Classes

Classes are permitted to perform basic operator overloading. The following
operators may be overloaded: 

    __bool__ - change truthiness of a variable
    __add__ - add two variables
    __sub__ - subtract two variables
    __mul__ - multiply two variables
    __div__ - divide two variables
    __mod__ - modulo two variables
    __pow__ - exponentiation two variables
    __eq__ - equality comparison
    __ne__ - inequality comparison
    __lt__ - less than comparison

### Native Code

**This is an internal feature.**

Wyrm is intended to be a 'self-hosted' language using 'C' as it's internal
assembly language. The special built-in module 'native' specifies the module
is intended for compilation:

import native

Importing native allows a module to create and use C functions. The import module
notifies the interpreter that the module is intended as a compiled wyrm extension.
Attempting to import a module at runtime will result in an error.

The block function defines a native block. It accepts a symbol specifying the
generation portion, a list of input symbols, a list of output symbols, and a
string parameter of content:

    native::block('HEADER, '(), '(), R"C(

    #include <stdio.h>
    #include <stdlib.h>

    )C")

Block parameters *MUST* be literals. The block function call is detected by the
compiler and used to feed directly into the generated code.

The following portions of a file are available:

    HEADER - includes, headers, definitions
    TYPES - type definitions
    CONSTANTS - constant definitions
    PROTOS - function prototypes
    FUNCTIONS - function definitions

Within a generated function, the block operator may be used to define a chunk of
C code. This chunk of C code will be inserted directly in the generated function.
The code chunk is placed within it's own dedicated scope. The list of input variables
is read, the chunk is placed, and the output variables are written.

    fn quadratic_formula(a, b, c) -> float, float:
        x: float
        y: float
        native::block('HEADER, '('a, 'b, 'c), '('x, 'y), R"C(
            x = (-b + sqrt(b*b - 4*a*c)) / (2*a)
            y = (-b - sqrt(b*b - 4*a*c)) / (2*a)
        )C"
        return x, y

Generated C block:

    wyrm_error w_mymodule_quadratic_formula(wyrm_state* state)
    {
        /* magic start */
        /* magic block start */
        {
           float x;
           float y;
   
           float a = /* magic */;
           float b = /* magic */;
           float c = /* magic */;
   
           x = (-b + sqrt(b*b - 4*a*c)) / (2*a)
           y = (-b - sqrt(b*b - 4*a*c)) / (2*a)
   
           /* magic */ = x;
           /* magic */ = y;
         }
        /* magic block end */
    }

Type Mapping:

 - int -> wyrm_word
 - float -> float
 - bool -> bool
 - str (input only) -> wyrm_string*
 - object -> wyrm_value
