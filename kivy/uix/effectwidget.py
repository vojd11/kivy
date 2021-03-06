'''
EffectWidget
============

.. versionadded:: 1.8.1

    This code is still experimental, and its API is subject to change in a
    future version.

The :class:`EffectWidget` is able to apply a variety of fancy
graphical effects to
its children. It works by rendering to a series of
:class:`~kivy.graphics.Fbo` instances with custom opengl fragment shaders.
As such, effects can freely do almost anything, from inverting the
colors of the widget, to antialiasing, to emulating the appearance of a
crt monitor!

The basic usage is as follows::

    w = EffectWidget()
    w.add_widget(Button(text='Hello!')
    w.effects = [InvertEffect(), HorizontalBlurEffect(size=2.0)]

The effects can be a list of effects of any length, and they will be
applied sequentially.

The module comes with a range of prebuilt effects, but the interface
is designed to make it easy to create your own. Instead of writing a
full glsl shader, you provide a single function that takes
some inputs based on the screen (current pixel color, current widget
texture etc.). See the sections below for more information.

.. note:: It is not efficient to resize an :class:`EffectWidget`, as
          each :class:`~kivy.graphics.Fbo` is recreated every time.
          If you need to resize frequently, consider doing things a
          different way.

.. note:: Although some effects have adjustable parameters, it is
          *not* efficient to animate these, as the entire
          shader is reconstructed every time. You should use glsl
          uniform variables instead. The :class:`AdvancedEffectBase`
          may make this easier.

.. note:: The :class:`EffectWidget` *cannot* draw outside its own
          widget area (pos -> pos + size), any child widgets
          overlapping the boundary will be cut off at this point.

Provided Effects
----------------

The module comes with several pre-written effects. Some have
adjustable properties (e.g. blur radius), see the individual
effect documentation for more details.

- :class:`MonochromeEffect` - makes the widget grayscale.
- :class:`InvertEffect` - inverts the widget colors.
- :class:`ChannelMixEffect` - swaps around color channels.
- :class:`ScanlinesEffect` - displays flickering scanlines.
- :class:`PixelateEffect` - pixelates the image.
- :class:`HorizontalBlurEffect` - Gaussuan blurs horizontally.
- :class:`VerticalBlurEffect` - Gaussuan blurs vertically.
- :class:`FXAAEffect` - applies a very basic AA.

Creating Effects
----------------

Effects are designed to make it easy to create and use your own
transformations. You do this by creating and using an instance of
:class:`EffectBase` with your own custom :attr:`EffectBase.glsl`
property.

The glsl property is a string representing part of a glsl fragment
shader. You can include as many functions as you like (the string
is simply spliced into the whole shader), but it
must implement a function :code:`effect` as below::

    vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
    {
        // ... your code here
        return something;  // must be a vec4 representing the new color
    }

The full shader will calculate the normal pixel colour at each point,
then call your :code:`effect` function to transform it. The
parameters are:

- **color**: The normal colour of the current pixel (i.e. texture
  sampled at tex_coords).
- **texture**: The texture containing the widget's normal background.
- **tex_coords**: The normal texture_coords used to access texture.
- **coords**: The pixel indices of the current pixel.

The shader code also has access to two useful uniform variables,
:code:`time` containing the time (in seconds) since the program start,
and :code:`resolution` containing the shape (x pixels, y pixels) of
the widget.

For instance, the following simple string (taken from the `InvertEffect`)
would invert the input color but set alpha to 1.0::

    vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
    {
        return vec4(1.0 - color.xyz, 1.0);
    }

You can also set the glsl by automatically loading the string from a
file, simply set the :attr:`EffectBase.source` property of an effect.

'''

from kivy.clock import Clock
from kivy.uix.relativelayout import RelativeLayout
from kivy.properties import (StringProperty, ObjectProperty, ListProperty,
                             NumericProperty, DictProperty)
from kivy.graphics import (RenderContext, Fbo, Color, Rectangle,
                           Translate, PushMatrix, PopMatrix)
from kivy.event import EventDispatcher
from kivy.base import EventLoop
from kivy.resources import resource_find

__all__ = ('EffectWidget', 'EffectBase', 'AdvancedEffectBase',
           'MonochromeEffect', 'InvertEffect', 'ChannelMixEffect',
           'ScanlinesEffect', 'PixelateEffect',
           'HorizontalBlurEffect', 'VerticalBlurEffect',
           'FXAAEffect')

shader_header = '''
#ifdef GL_ES
precision highp float;
#endif

/* Outputs from the vertex shader */
varying vec4 frag_color;
varying vec2 tex_coord0;

/* uniform texture samplers */
uniform sampler2D texture0;
'''

shader_uniforms = '''
uniform vec2 resolution;
uniform float time;
'''

shader_footer_trivial = '''
void main (void){
     gl_FragColor = frag_color * texture2D(texture0, tex_coord0);
}
'''

shader_footer_effect = '''
void main (void){
    vec4 normal_color = frag_color * texture2D(texture0, tex_coord0);
    vec4 effect_color = effect(normal_color, texture0, tex_coord0,
                               gl_FragCoord.xy);
    gl_FragColor = effect_color;
}
'''


effect_trivial = '''
vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
{
    return color;
}
'''

effect_monochrome = '''
vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
{
    float mag = 1.0/3.0 * (color.x + color.y + color.z);
    return vec4(mag, mag, mag, color.w);
}
'''

effect_invert = '''
vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
{
    return vec4(1.0 - color.xyz, 1.0);
}
'''

effect_mix = '''
vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
{{
    return vec4(color.{}, color.{}, color.{}, 1.0);
}}
'''

effect_blur_h = '''
vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
{{
    float dt = ({} / 4.0) * 1.0 / resolution.x;
    vec4 sum = vec4(0.0);
    sum += texture2D(texture, vec2(tex_coords.x - 4.0*dt, tex_coords.y))
                     * 0.05;
    sum += texture2D(texture, vec2(tex_coords.x - 3.0*dt, tex_coords.y))
                     * 0.09;
    sum += texture2D(texture, vec2(tex_coords.x - 2.0*dt, tex_coords.y))
                     * 0.12;
    sum += texture2D(texture, vec2(tex_coords.x - dt, tex_coords.y))
                     * 0.15;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y))
                     * 0.16;
    sum += texture2D(texture, vec2(tex_coords.x + dt, tex_coords.y))
                     * 0.15;
    sum += texture2D(texture, vec2(tex_coords.x + 2.0*dt, tex_coords.y))
                     * 0.12;
    sum += texture2D(texture, vec2(tex_coords.x + 3.0*dt, tex_coords.y))
                     * 0.09;
    sum += texture2D(texture, vec2(tex_coords.x + 4.0*dt, tex_coords.y))
                     * 0.05;
    return sum;
}}
'''

effect_blur_v = '''
vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
{{
    float dt = ({} / 4.0)
                     * 1.0 / resolution.x;
    vec4 sum = vec4(0.0);
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y - 4.0*dt))
                     * 0.05;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y - 3.0*dt))
                     * 0.09;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y - 2.0*dt))
                     * 0.12;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y - dt))
                     * 0.15;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y))
                     * 0.16;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y + dt))
                     * 0.15;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y + 2.0*dt))
                     * 0.12;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y + 3.0*dt))
                     * 0.09;
    sum += texture2D(texture, vec2(tex_coords.x, tex_coords.y + 4.0*dt))
                     * 0.05;
    return sum;
}}
'''

effect_postprocessing = '''
vec4 effect(vec4 color, sampler2D texture, vec2 tex_coords, vec2 coords)
{
    vec2 q = tex_coords * vec2(1, -1);
    vec2 uv = 0.5 + (q-0.5);//*(0.9);// + 0.1*sin(0.2*time));

    vec3 oricol = texture2D(texture,vec2(q.x,1.0-q.y)).xyz;
    vec3 col;

    col.r = texture2D(texture,vec2(uv.x+0.003,-uv.y)).x;
    col.g = texture2D(texture,vec2(uv.x+0.000,-uv.y)).y;
    col.b = texture2D(texture,vec2(uv.x-0.003,-uv.y)).z;

    col = clamp(col*0.5+0.5*col*col*1.2,0.0,1.0);

    //col *= 0.5 + 0.5*16.0*uv.x*uv.y*(1.0-uv.x)*(1.0-uv.y);

    col *= vec3(0.8,1.0,0.7);

    col *= 0.9+0.1*sin(10.0*time+uv.y*1000.0);

    col *= 0.97+0.03*sin(110.0*time);

    float comp = smoothstep( 0.2, 0.7, sin(time) );
    //col = mix( col, oricol, clamp(-2.0+2.0*q.x+3.0*comp,0.0,1.0) );

    return vec4(col,1.0);
}
'''

effect_pixelate = '''
vec4 effect(vec4 vcolor, sampler2D texture, vec2 texcoord, vec2 pixel_coords)
{{
    vec2 pixelSize = {} / resolution;

    vec2 xy = floor(texcoord/pixelSize)*pixelSize + pixelSize/2.0;

    return texture2D(texture, xy);
}}
'''

effect_fxaa = '''
vec4 effect( vec4 color, sampler2D buf0, vec2 texCoords, vec2 coords)
{

    vec2 frameBufSize = resolution;

    float FXAA_SPAN_MAX = 8.0;
    float FXAA_REDUCE_MUL = 1.0/8.0;
    float FXAA_REDUCE_MIN = 1.0/128.0;

    vec3 rgbNW=texture2D(buf0,texCoords+(vec2(-1.0,-1.0)/frameBufSize)).xyz;
    vec3 rgbNE=texture2D(buf0,texCoords+(vec2(1.0,-1.0)/frameBufSize)).xyz;
    vec3 rgbSW=texture2D(buf0,texCoords+(vec2(-1.0,1.0)/frameBufSize)).xyz;
    vec3 rgbSE=texture2D(buf0,texCoords+(vec2(1.0,1.0)/frameBufSize)).xyz;
    vec3 rgbM=texture2D(buf0,texCoords).xyz;

    vec3 luma=vec3(0.299, 0.587, 0.114);
    float lumaNW = dot(rgbNW, luma);
    float lumaNE = dot(rgbNE, luma);
    float lumaSW = dot(rgbSW, luma);
    float lumaSE = dot(rgbSE, luma);
    float lumaM  = dot(rgbM,  luma);

    float lumaMin = min(lumaM, min(min(lumaNW, lumaNE), min(lumaSW, lumaSE)));
    float lumaMax = max(lumaM, max(max(lumaNW, lumaNE), max(lumaSW, lumaSE)));

    vec2 dir;
    dir.x = -((lumaNW + lumaNE) - (lumaSW + lumaSE));
    dir.y =  ((lumaNW + lumaSW) - (lumaNE + lumaSE));

    float dirReduce = max(
        (lumaNW + lumaNE + lumaSW + lumaSE) * (0.25 * FXAA_REDUCE_MUL),
        FXAA_REDUCE_MIN);

    float rcpDirMin = 1.0/(min(abs(dir.x), abs(dir.y)) + dirReduce);

    dir = min(vec2( FXAA_SPAN_MAX,  FXAA_SPAN_MAX),
          max(vec2(-FXAA_SPAN_MAX, -FXAA_SPAN_MAX),
          dir * rcpDirMin)) / frameBufSize;

    vec3 rgbA = (1.0/2.0) * (
        texture2D(buf0, texCoords.xy + dir * (1.0/3.0 - 0.5)).xyz +
        texture2D(buf0, texCoords.xy + dir * (2.0/3.0 - 0.5)).xyz);
    vec3 rgbB = rgbA * (1.0/2.0) + (1.0/4.0) * (
        texture2D(buf0, texCoords.xy + dir * (0.0/3.0 - 0.5)).xyz +
        texture2D(buf0, texCoords.xy + dir * (3.0/3.0 - 0.5)).xyz);
    float lumaB = dot(rgbB, luma);

    vec4 return_color;
    if((lumaB < lumaMin) || (lumaB > lumaMax)){
        return_color = vec4(rgbA, color.w);
    }else{
        return_color = vec4(rgbB, color.w);
    }

    return return_color;
}
'''


class EffectBase(EventDispatcher):
    '''The base class for GLSL effects. It simply returns its input.

    See module documentation for more details.

    '''

    glsl = StringProperty(effect_trivial)
    '''The glsl string defining your effect function, see module
    documentation for more details.

    :attr:`glsl` is a :class:`~kivy.properties.StringProperty` and
    defaults to
    a trivial effect that returns its input.
    '''

    source = StringProperty('')
    '''The (optional) filename from which to load the :attr:`glsl`
    string.

    :attr:`source` is a :class:`~kivy.properties.StringProperty` and
    defaults to ''.
    '''

    fbo = ObjectProperty(None, allownone=True)
    '''The fbo currently using this effect. The :class:`EffectBase`
    automatically handles this.

    :attr:`fbo` is a :class:`~kivy.properties.ObjectProperty` and
    defaults to None.
    '''

    def __init__(self, *args, **kwargs):
        super(EffectBase, self).__init__(*args, **kwargs)
        self.bind(fbo=self.set_fbo_shader)
        self.bind(glsl=self.set_fbo_shader)
        self.bind(source=self._load_from_source)

    def set_fbo_shader(self, *args):
        '''Sets the :class:`~kivy.graphics.Fbo`'s shader by splicing
        the :attr:`glsl` string into a full fragment shader.

        The full shader is made up of :code:`shader_header +
        shader_uniforms + self.glsl + shader_footer_effect`.
        '''
        if self.fbo is None:
            return
        self.fbo.set_fs(shader_header + shader_uniforms + self.glsl +
                        shader_footer_effect)

    def _load_from_source(self, *args):
        '''(internal) Loads the glsl string from a source file.'''
        source = self.source
        if not source:
            return
        filename = resource_find(source)
        if filename is None:
            return Logger.error('Error reading file {filename}'.
                                format(filename=source))
        with open(filename) as fileh:
            self.glsl = fileh.read()


class AdvancedEffectBase(EffectBase):
    '''An :class:`EffectBase` with additional behavior to easily
    set and update uniform variables in your shader.

    This class is provided for convenience if implementing your own
    effects, it is not used by any of those provided with Kivy.

    In addition to your base glsl string that must be provided as
    normal, the :class:`AdvancedEffectBase` has an extra property
    :attr:`uniforms`, a dictionary of name-value pairs. Whenever
    a value is changed, the new values for the uniform variable with
    the given name are uploaded to the shader.

    You must still manually declare your uniform variables at the top
    of your glsl string.
    '''

    uniforms = DictProperty({})
    '''A dictionary of uniform variable names and their values. These
    are automatically uploaded to the :attr:`fbo` shader if appropriate.

    uniforms is a :class:`~kivy.properties.DictProperty` and
    defaults to {}.
    '''

    def __init__(self, *args, **kwargs):
        super(AdvancedEffectBase, self).__init__(*args, **kwargs)
        self.bind(uniforms=self._update_uniforms)

    def _update_uniforms(self, *args):
        if self.fbo is None:
            return
        for key, value in self.uniforms.items():
            self.fbo[key] = value

    def set_fbo_shader(self, *args):
        super(AdvancedEffectBase, self).set_fbo_shader(*args)
        self._update_uniforms()


class MonochromeEffect(EffectBase):
    '''Returns its input colours in monochrome.'''
    def __init__(self, *args, **kwargs):
        super(MonochromeEffect, self).__init__(*args, **kwargs)
        self.glsl = effect_monochrome


class InvertEffect(EffectBase):
    '''Inverts the colours in the input.'''
    def __init__(self, *args, **kwargs):
        super(InvertEffect, self).__init__(*args, **kwargs)
        self.glsl = effect_invert


class ScanlinesEffect(EffectBase):
    '''Adds scanlines to the input.'''
    def __init__(self, *args, **kwargs):
        super(ScanlinesEffect, self).__init__(*args, **kwargs)
        self.glsl = effect_postprocessing


class ChannelMixEffect(EffectBase):
    '''Mixes the color channels of the input according to the order
    property. Channels may be arbitrarily rearranged or repeated.'''

    order = ListProperty([1, 2, 0])
    '''The new sorted order of the rgb channels.

    order is a :class:`~kivy.properties.ListProperty` and defaults to
    [1, 2, 0], corresponding to (g, b, r).
    '''

    def __init__(self, *args, **kwargs):
        super(ChannelMixEffect, self).__init__(*args, **kwargs)
        self.do_glsl()

    def on_order(self, *args):
        self.do_glsl()

    def do_glsl(self):
        letters = [{0: 'x', 1: 'y', 2: 'z'}[i] for i in self.order]
        self.glsl = effect_mix.format(*letters)


class PixelateEffect(EffectBase):
    '''Pixelates the input according to its
    :attr:`~PixelateEffect.pixel_size`'''

    pixel_size = NumericProperty(10)
    '''
    Sets the size of a new 'pixel' in the effect, in terms of number of
    'real' pixels.

    pixel_size is a :class:`~kivy.properties.NumericProperty` and
    defaults to 10.
    '''

    def __init__(self, *args, **kwargs):
        super(PixelateEffect, self).__init__(*args, **kwargs)
        self.do_glsl()

    def on_pixel_size(self, *args):
        self.do_glsl()

    def do_glsl(self):
        self.glsl = effect_pixelate.format(float(self.pixel_size))


class HorizontalBlurEffect(EffectBase):
    '''Blurs the input horizontally, with the width given by
    :attr:`~HorizontalBlurEffect.size`.'''

    size = NumericProperty(4.0)
    '''The blur width in pixels.

    size is a :class:`~kivy.properties.NumericProperty` and defaults to
    4.0.
    '''

    def __init__(self, *args, **kwargs):
        super(HorizontalBlurEffect, self).__init__(*args, **kwargs)
        self.do_glsl()

    def on_size(self, *args):
        self.do_glsl()

    def do_glsl(self):
        self.glsl = effect_blur_h.format(float(self.size))


class VerticalBlurEffect(EffectBase):
    '''Blurs the input vertically, with the width given by
    :attr:`~VerticalBlurEffect.size`.'''

    size = NumericProperty(4.0)
    '''The blur width in pixels.

    size is a :class:`~kivy.properties.NumericProperty` and defaults to
    4.0.
    '''

    def __init__(self, *args, **kwargs):
        super(VerticalBlurEffect, self).__init__(*args, **kwargs)
        self.do_glsl()

    def on_size(self, *args):
        self.do_glsl()

    def do_glsl(self):
        self.glsl = effect_blur_v.format(float(self.size))


class FXAAEffect(EffectBase):
    '''Applies very simple antialiasing via fxaa.'''
    def __init__(self, *args, **kwargs):
        super(FXAAEffect, self).__init__(*args, **kwargs)
        self.glsl = effect_fxaa


class EffectFbo(Fbo):
    '''An :class:`~kivy.graphics.Fbo` with extra facility to
    attempt setting a new shader, see :meth:`set_fs`.
    '''
    def __init__(self, *args, **kwargs):
        super(EffectFbo, self).__init__(*args, **kwargs)
        self.texture_rectangle = None

    def set_fs(self, value):
        '''Attempt to set the fragment shader to the given value.
        If setting the shader fails, resets the old one and raises an
        exception.
        '''
        shader = self.shader
        old_value = shader.fs
        shader.fs = value
        if not shader.success:
            shader.fs = old_value
            raise Exception('Setting new shader failed.')


class EffectWidget(RelativeLayout):
    '''
    Widget with the ability to apply a series of graphical effects to
    its children. See module documentation for full information on
    setting effects and creating your own.
    '''

    background_color = ListProperty((1, 1, 1, 1))
    '''This defines the background color to be used for the fbo in the
    EffectWidget.

    :attr:`background_color` is a :class:`ListProperty` defaults to (1, 1, 1, 1)
    '''

    texture = ObjectProperty(None)
    '''The output texture of our final :class:`~kivy.graphics.Fbo` after
    all effects have been applied.

    texture is an :class:`~kivy.properties.ObjectProperty` and defaults
    to None.
    '''

    effects = ListProperty([])
    '''List of all the effects to be applied. These should all be
    instances of :class:`EffectBase`.

    effects is a :class:`ListProperty` and defaults to [].
    '''

    fbo_list = ListProperty([])
    '''(internal) list of all the fbos that are being used to apply
    the effects.

    fbo_list is a :class:`ListProperty` and defaults to [].
    '''

    _bound_effects = ListProperty([])
    '''(internal) list of effect classes that have been given an fbo to
    manage. This is necessary so that the fbo can be removed it the
    effect is no longer in use.

    _bound_effects is a :class:`ListProperty` and defaults to [].
    '''

    def __init__(self, **kwargs):
        # Make sure opengl context exists
        EventLoop.ensure_window()

        self.canvas = RenderContext(use_parent_projection=True,
                                    use_parent_modelview=True)

        with self.canvas:
            self.fbo = Fbo(size=self.size)

        with self.fbo.before:
            PushMatrix()
            self.fbo_translation = Translate(-self.x, -self.y, 0)
        with self.fbo:
            Color(*self.background_color)
            self.fbo_rectangle = Rectangle(size=self.size)
        with self.fbo.after:
            PopMatrix()

        super(EffectWidget, self).__init__(**kwargs)

        Clock.schedule_interval(self._update_glsl, 0)

        self.bind(pos=self._update_translation,
                  size=self.refresh_fbo_setup,
                  effects=self.refresh_fbo_setup)

        self.refresh_fbo_setup()

    def _update_translation(self, *args):
        '''(internal) Makes sure everything is translated correctly to
        appear in the fbo.'''
        self.fbo_translation.x = -self.x
        self.fbo_translation.y = -self.y

    def _update_glsl(self, *largs):
        '''(internal) Passes new time and resolution uniform
        variables to the shader.
        '''
        time = Clock.get_boottime()
        resolution = [float(size) for size in self.size]
        self.canvas['time'] = time
        self.canvas['resolution'] = resolution
        for fbo in self.fbo_list:
            fbo['time'] = time
            fbo['resolution'] = resolution

    def refresh_fbo_setup(self, *args):
        '''(internal) Creates and assigns one :class:`~kivy.graphics.Fbo`
        per effect, and makes sure all sizes etc. are correct and
        consistent.
        '''
        # Add/remove fbos until there is one per effect
        while len(self.fbo_list) < len(self.effects):
            with self.canvas:
                new_fbo = EffectFbo(size=self.size)
            with new_fbo:
                Color(*self.background_color)
                new_fbo.texture_rectangle = Rectangle(
                    size=self.size)

                new_fbo.texture_rectangle.size = self.size
            self.fbo_list.append(new_fbo)
        while len(self.fbo_list) > len(self.effects):
            old_fbo = self.fbo_list.pop()
            self.canvas.remove(old_fbo)

        # Remove fbos from unused effects
        for effect in self._bound_effects:
            if effect not in self.effects:
                effect.fbo = None
        self._bound_effects = self.effects

        # Do resizing etc.
        self.fbo.size = self.size
        self.fbo_rectangle.size = self.size
        for i in range(len(self.fbo_list)):
            self.fbo_list[i].size = self.size
            self.fbo_list[i].texture_rectangle.size = self.size

        # If there are no effects, just draw our main fbo
        if len(self.fbo_list) == 0:
            self.texture = self.fbo.texture
            return

        for i in range(1, len(self.fbo_list)):
            fbo = self.fbo_list[i]
            fbo.texture_rectangle.texture = self.fbo_list[i - 1].texture

        # Build effect shaders
        for effect, fbo in zip(self.effects, self.fbo_list):
            effect.fbo = fbo

        self.fbo_list[0].texture_rectangle.texture = self.fbo.texture
        self.texture = self.fbo_list[-1].texture

    def add_widget(self, widget):
        # Add the widget to our Fbo instead of the normal canvas
        c = self.canvas
        self.canvas = self.fbo
        super(EffectWidget, self).add_widget(widget)
        self.canvas = c

    def remove_widget(self, widget):
        # Remove the widget from our Fbo instead of the normal canvas
        c = self.canvas
        self.canvas = self.fbo
        super(EffectWidget, self).remove_widget(widget)
        self.canvas = c

    def clear_widgets(self, children=None):
        # Clear widgets from our Fbo instead of the normal canvas
        c = self.canvas
        self.canvas = self.fbo
        super(EffectWidget, self).clear_widgets(children)
        self.canvas = c
